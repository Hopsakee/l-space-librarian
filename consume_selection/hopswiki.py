"""cs-hopswiki — Hopswiki reading-anchors + syntheses → consume.db.

Hopswiki is Jelle's own knowledge wiki. Two things belong in the firehose so they
rank in the same answer as Readwise/books/podcasts:

  * `sources/` notes (the reading anchors his syntheses cite) → **candidates**
    (`consumed=0`). These are the "toReads from Hopswiki" — concrete reading
    material referenced by his wiki that he may not have fully read.
  * `syntheses/` + `comparisons/` (his OWN processed knowledge) → **known rows**
    (`consumed=1`), so "what have I already synthesised" is queryable in the same
    catalogue WITHOUT polluting the new-to-consume lane (candidates filter consumed=0).

`concepts/` (hundreds of atomic knowledge notes) are deliberately NOT ingested — they
are knowledge atoms, not consumables, and would flood the firehose. They remain
searchable via _READING_ADVICE's live Hopswiki lane.

NOTE on Recall: the other half of Jelle's REVISIT archive lives in recall.it, an MCP
tool reachable only from inside a Hoggle session — NOT from a deterministic nightly
script. So Recall is intentionally NOT ingested here; _READING_ADVICE already searches
it LIVE at advice time (the REVISIT lane). This adapter covers Hopswiki only.

Read-only over the vault; writes only the local consume.db. Reuses the shared
`ingest.upsert_row` so the upsert SQL never drifts.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema
from consume_selection.ingest import upsert_row

HOPSWIKI = Path.home() / "Obsidian" / "Hopswiki" / "Hopswiki"
SOURCE_DIR = "sources"                       # reading anchors → candidates
KNOWN_DIRS = ("syntheses", "comparisons")    # own processed knowledge → consumed=1

_FM = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.S)
_TITLE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.M)
_TYPE = re.compile(r"^type:\s*(.+)$", re.M)
_URL = re.compile(r"^url:\s*(.+)$", re.M)
_AUTHOR = re.compile(r"^author:\s*\"?(.*?)\"?\s*$", re.M)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(path: Path) -> dict:
    """Minimal frontmatter + body parse (no YAML dep). Returns title/type/url/author/body."""
    text = path.read_text(encoding="utf-8")
    m = _FM.match(text)
    fm, body = (m.group(1), m.group(2)) if m else ("", text)
    tm, ty, um, am = _TITLE.search(fm), _TYPE.search(fm), _URL.search(fm), _AUTHOR.search(fm)
    return {
        "title": (tm.group(1).strip() if tm else path.stem),
        "type": (ty.group(1).strip() if ty else ""),
        "url": (um.group(1).strip() if um else None),
        "author": (am.group(1).strip() if am else None),
        "body": body.strip(),
    }


def _summary(meta: dict) -> str:
    """A BM25-usable summary: title + a body excerpt (markdown/links stripped-ish)."""
    body = re.sub(r"[#>*`\[\]]", " ", meta["body"])
    body = re.sub(r"\s+", " ", body).strip()
    return f"{meta['title']}. {body[:600]}".strip()


def _row(path: Path, meta: dict, *, consumed: int, item_type: str) -> dict:
    rel = path.relative_to(HOPSWIKI)
    return {
        "id": "hopswiki-" + hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:16],
        "source": "hopswiki",
        "title": meta["title"],
        "author": meta.get("author"),
        "url": meta.get("url"),
        "source_url": meta.get("url"),
        "site_name": "hopswiki",
        "summary": _summary(meta),
        "item_type": item_type,
        "word_count": None,
        "location": str(path),
        "added_at": None,
        "ingested_at": _now(),
        "consumed": consumed,
        "consumed_at": None,
    }


def _scan(subdir: str) -> list[Path]:
    d = HOPSWIKI / subdir
    return sorted(d.rglob("*.md")) if d.exists() else []


@call_parse
def main(
    dry_run: bool = False,   # scan + report only, write nothing
    db: str = "",            # consume.db path override (tests use a temp db)
):
    "Ingest Hopswiki sources/ as reading candidates + syntheses/comparisons as known rows. concepts/ skipped; Recall is searched live by _READING_ADVICE, not ingested."
    if not HOPSWIKI.exists():
        raise SystemExit(f"Hopswiki vault not found: {HOPSWIKI}")

    conn = connect(db or None)
    init_schema(conn)

    sources = known = 0
    # Reading anchors → candidates (consumed=0). Their frontmatter `type` (source/
    # article/paper/…) isn't a consume-type; map everything textual to 'article'.
    for p in _scan(SOURCE_DIR):
        if not dry_run:
            upsert_row(conn, _row(p, _parse(p), consumed=0, item_type="article"),
                       extra_cols=("consumed", "consumed_at"))
        sources += 1
    # Own syntheses/comparisons → known rows (consumed=1), never candidates.
    for sub in KNOWN_DIRS:
        for p in _scan(sub):
            if not dry_run:
                upsert_row(conn, _row(p, _parse(p), consumed=1, item_type="synthesis"),
                           extra_cols=("consumed", "consumed_at"))
            known += 1

    if not dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM items WHERE source='hopswiki'").fetchone()[0]
    conn.close()

    print(f"hopswiki sources(candidates)={sources} known(syntheses+comparisons)={known} "
          f"dry_run={dry_run}")
    print(f"hopswiki rows now {total} (concepts/ intentionally skipped; Recall searched live)")
    if not dry_run:
        print("reminder: run `cs-fts --rebuild` so BM25 sees the new rows")
