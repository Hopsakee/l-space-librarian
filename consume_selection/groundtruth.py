"""cs-groundtruth — pull every `_rating/*` tag into consume.db `ratings`.

The ISA assumed a `rating/<tier>/zelf` (Jelle's own) vs `rating/<tier>/chatgpt 5.1`
(auto) dual-tagging. The real data has neither: tags are `_rating/<tier>` (bare,
n8n era) or `_rating/<tier>/<model>` (e.g. `_rating/s/mistrall-small-4`), and the
two NEVER co-occur. So this puller is generic: it records EVERY rating tag,
keyed by rater (`bare` or the model name), preserving the original tag string.
Which rater string counts as Jelle's ground-truth is a decision left to Jelle.

Reuses ingest.upsert_items so the referenced item rows exist before ratings are
written (FK-safe). Idempotent: re-running REPLACEs each (item_id, rater) row.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema
from consume_selection.ingest import upsert_items, _load_docs

# Matches `_rating/<tier>` and `_rating/<tier>/<rater>`; leading underscore optional.
_RATING_RE = re.compile(r"^_?rating/([a-z]+)(?:/(.+))?$", re.IGNORECASE)
BARE_RATER = "bare"


def parse_ratings(tags: dict) -> list[tuple[str, str, str]]:
    """From a Readwise tags dict, yield (tier, rater, raw_tag) for each rating tag."""
    out: list[tuple[str, str, str]] = []
    for key, meta in (tags or {}).items():
        m = _RATING_RE.match(key)
        if not m:
            continue
        tier = m.group(1).lower()
        rater = (m.group(2) or BARE_RATER).lower()
        raw = (meta or {}).get("name") or key  # .name preserves original case
        out.append((tier, rater, raw))
    return out


def pull(conn, docs: list[dict]) -> dict:
    """Ensure item rows exist, then upsert their rating tags. Returns a breakdown."""
    upsert_items(conn, [d for d in docs if d.get("id")])
    rows = 0
    rated_items = 0
    by_rater: dict[str, int] = {}
    for d in docs:
        if not d.get("id"):
            continue          # id-less doc: cannot key a rating row; skip (matches upsert_items filter)
        ratings = parse_ratings(d.get("tags") or {})
        if not ratings:
            continue
        rated_items += 1
        for tier, rater, raw in ratings:
            conn.execute(
                "INSERT INTO ratings (item_id, tier, rater, raw_tag) VALUES (?,?,?,?) "
                "ON CONFLICT(item_id, rater) DO UPDATE SET tier=excluded.tier, raw_tag=excluded.raw_tag",
                (d["id"], tier, rater, raw),
            )
            rows += 1
            by_rater[rater] = by_rater.get(rater, 0) + 1
    conn.commit()
    return {"rated_items": rated_items, "rating_rows": rows, "by_rater": by_rater}


@call_parse
def main(
    location: str = "later",  # Reader location to scan when fetching live
    from_json: str = "",      # use a cached rw-list dump (must include the `tags` field)
    limit: int = 0,
    db: str = "",
):
    "Pull every `_rating/*` tag from Readwise into consume.db `ratings`."
    conn = connect(db or None)
    init_schema(conn)
    docs = _load_docs(location, from_json, limit)
    stats = pull(conn, docs)
    total = conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
    conn.close()
    print(f"rated items: {stats['rated_items']}, rating rows written: {stats['rating_rows']}")
    print(f"by rater: {json.dumps(stats['by_rater'], ensure_ascii=False)}")
    print(f"ratings table now holds {total} rows")
