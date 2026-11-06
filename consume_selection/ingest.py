"""cs-ingest — write Readwise documents into consume.db `items`.

Reuses the readwise-tools `ReaderClient` (same code path as the rw-list CLI) for
live fetches; `--from-json` ingests a cached rw-list dump instead (rate-friendly,
deterministic for tests).

Idempotency contract (ISC-6/ISC-10): re-ingest UPSERTs only the ingest-owned
columns. Enrichment columns written by later slices — quality_auto, quality_self,
topic_tags, embedding, consumed, consumed_at — are NEVER touched here, so
re-ingesting never clobbers an embedding or a score. One exception: a podcast in
Readwise is one Jelle has heard, so its row carries consumed=1 like the other
adapters' read-state (librarything, toread).

Reconcile (added 2026-11-06): a document Jelle archives or deletes in Reader stops
appearing in the `new`/`later` fetch, so without a reconcile its row said `later`
forever (159 such rows measured on 2026-11-06). `--reconcile` marks every live
readwise row that a COMPLETE fetch of both `new` and `later` did not return as
`GONE: <last location>` — the prefix `toread.py` already uses, which
`consume_list.py` filters with `NOT LIKE 'GONE:%'`. The row is never deleted, and a
document that shows up again gets its real location back on the next upsert. A
fetch that failed, was capped with `--limit`, or did not cover both locations
marks nothing: a truncated fetch must never make live items look gone.

Anti (ISC-7): this module imports nothing from recall.it / Hopswiki and makes no
such calls. It only reads Readwise and writes the local consume.db.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema

# Columns refreshed on every ingest (metadata can legitimately change in Reader).
# Deliberately excludes the enrichment columns so re-ingest preserves them.
_INGEST_COLS = [
    "source", "title", "author", "url", "source_url", "site_name",
    "summary", "item_type", "word_count", "location", "added_at", "ingested_at",
]


def upsert_row(conn, row: dict, *, extra_cols: tuple[str, ...] = ()) -> None:
    """Idempotent single-row upsert into `items`, shared by every source adapter so
    the INSERT…ON CONFLICT tail lives in ONE place (was copy-pasted across
    librarything/toread/podcasts — /simplify 2026-07-05). Writes id + the ingest-owned
    columns (`_INGEST_COLS`) + the adapter's `extra_cols` (e.g. consumed/consumed_at,
    duration_minutes); enrichment columns (embedding, quality_*, topic_tags) are never
    in the write set, so re-ingest never clobbers them. `row` must carry every written key."""
    cols = ["id"] + _INGEST_COLS + list(extra_cols)
    placeholders = ", ".join(["?"] * len(cols))
    update_set = ", ".join(f"{c}=excluded.{c}" for c in _INGEST_COLS + list(extra_cols))
    conn.execute(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set}",
        [row[c] for c in cols],
    )


def _row_from_doc(doc: dict, now: str) -> dict:
    """Map a Readwise document object to an items row dict."""
    return {
        "id": doc["id"],
        "source": "readwise",
        "title": doc.get("title"),
        "author": doc.get("author"),
        "url": doc.get("url"),
        "source_url": doc.get("source_url"),
        "site_name": doc.get("site_name"),
        "summary": doc.get("summary"),
        "item_type": doc.get("category"),
        "word_count": doc.get("word_count"),
        "location": doc.get("location"),
        "added_at": doc.get("created_at"),
        "ingested_at": now,
        "reading_progress": doc.get("reading_progress"),
        # Read-state, written only for podcasts (see upsert_items).
        "consumed": 1,
        "consumed_at": doc.get("created_at"),
    }


def upsert_items(conn, docs: list[dict]) -> tuple[int, int]:
    """Insert new items, refresh ingest-owned fields on existing ones.

    Returns (rows_seen, rows_new). Enrichment columns are never written here; a
    podcast also writes its read-state, because being in Readwise means heard.
    `reading_progress` is Reader's own state, so it is refreshed on every upsert.
    """
    now = datetime.now(timezone.utc).isoformat()
    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    seen = 0
    for doc in docs:
        if not doc.get("id"):
            continue
        row = _row_from_doc(doc, now)
        heard = row["item_type"] == "podcast"
        extra = ("reading_progress",) + (("consumed", "consumed_at") if heard else ())
        upsert_row(conn, row, extra_cols=extra)
        seen += 1
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    return seen, after - before


def _load_docs(location: str, from_json: str, limit: int) -> list[dict]:
    """Get documents from a cached rw-list dump or a live ReaderClient fetch."""
    if from_json:
        docs = json.loads(Path(from_json).expanduser().read_text())
        return docs[:limit] if limit else docs
    # Live: reuse the exact rw-list code path.
    from readwise_tools.client import ReaderClient
    return ReaderClient().fetch(location=location or None, limit=limit or None)


# --- reconcile ----------------------------------------------------------------

# Reader locations a readwise row can be live in. Only rows in one of these are
# reconciled; `archive`/`feed` rows (none today) and rows already GONE are left alone.
LIVE_LOCATIONS = ("new", "later", "shortlist")
# The locations a reconcile needs fetched IN FULL before it may conclude that an
# unseen document left them. These are the two the nightly ingest fetches.
RECONCILE_REQUIRES = frozenset({"new", "later"})
# Shared with toread.py's delist, so `NOT LIKE 'GONE:%'` filters both.
GONE_PREFIX = "GONE: "


def reconcile_refusal(fetched: set[str], failed: list[str], limit: int,
                      seen_ids: set[str]) -> str | None:
    """Why a reconcile must NOT run, or None when it may. Every reason is a way the
    fetch could be incomplete, and an incomplete fetch would mark live items gone."""
    if failed:
        return f"fetch failed for {', '.join(failed)}"
    if limit:
        return f"--limit {limit} caps the fetch, so it is not complete"
    missing = RECONCILE_REQUIRES - fetched
    if missing:
        return (f"no complete fetch of {', '.join(sorted(missing))} "
                "(a --from-json dump counts only when given as location=path)")
    if not seen_ids:
        # An empty Reader is far less likely than a broken fetch or an empty dump.
        return "the fetch returned no documents at all"
    return None


def mark_gone(conn, seen_ids: set[str]) -> int:
    """Prefix `GONE: ` to every live readwise row whose id is not in `seen_ids`.

    Call ONLY after a complete fetch (see `reconcile_refusal`). Keeps the previous
    location after the prefix, never deletes a row, and touches no other column —
    `ingested_at` stays the time the row was last seen in Reader. Returns the count."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen_ids (id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _seen_ids")
    conn.executemany("INSERT OR IGNORE INTO _seen_ids VALUES (?)", [(i,) for i in seen_ids])
    marks = ", ".join("?" * len(LIVE_LOCATIONS))
    cur = conn.execute(
        f"UPDATE items SET location = ? || location WHERE source = 'readwise' "
        f"AND location IN ({marks}) AND id NOT IN (SELECT id FROM _seen_ids)",
        (GONE_PREFIX, *LIVE_LOCATIONS))
    conn.execute("DROP TABLE _seen_ids")
    conn.commit()
    return cur.rowcount


def _sources(location: str, from_json: str) -> list[tuple[str, str]]:
    """(location, dump path) pairs to load. A `--from-json` entry is `path` or
    `location=path`, comma-separated; only the second form says which Reader
    location the dump holds, which a reconcile needs. Live: one pair per location."""
    if not from_json:
        return [(loc.strip(), "") for loc in location.split(",") if loc.strip()] or [("", "")]
    out = []
    for entry in from_json.split(","):
        loc, sep, path = entry.strip().partition("=")
        out.append((loc, path) if sep else ("", loc))
    return out


@call_parse
def main(
    location: str = "new",   # Reader location(s) to fetch live, comma-separated (e.g. new,later)
    from_json: str = "",     # cached rw-list dump(s) instead of a live fetch: path or location=path, comma-separated
    limit: int = 0,          # cap docs ingested per location (0 = all; >0 disables --reconcile)
    db: str = "",            # consume.db path override
    reconcile: bool = False, # after a complete fetch of new AND later, mark unseen live readwise rows GONE
):
    "Ingest Readwise documents into consume.db `items` (idempotent upsert), optionally reconciling vanished ones."
    conn = connect(db or None)
    init_schema(conn)
    docs, fetched, failed = [], set(), []
    for loc, path in _sources(location, from_json):
        label = f"{loc or '?'}={path}" if path else (loc or "all")
        try:
            got = _load_docs(loc, path, limit)
        except Exception as e:  # noqa: BLE001 — one failed location must not hide the others' upserts
            failed.append(label)
            print(f"fetch {label} FAILED: {str(e)[:200]}", file=sys.stderr)
            continue
        docs.extend(got)
        if loc:
            fetched.add(loc)
    seen, new = upsert_items(conn, docs)
    gone_msg = ""
    if reconcile:
        seen_ids = {d["id"] for d in docs if d.get("id")}
        why = reconcile_refusal(fetched, failed, limit, seen_ids)
        gone_msg = (f"; reconcile skipped: {why}" if why
                    else f"; reconcile marked {mark_gone(conn, seen_ids)} rows GONE")
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    src = f"cache {from_json}" if from_json else f"live location={location}"
    print(f"ingested from {src}: {seen} docs seen, {new} new, {total} total in items{gone_msg}")
    if failed:
        sys.exit(1)
