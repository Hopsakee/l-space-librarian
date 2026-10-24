"""cs-ingest — write Readwise documents into consume.db `items`.

Reuses the readwise-tools `ReaderClient` (same code path as the rw-list CLI) for
live fetches; `--from-json` ingests a cached rw-list dump instead (rate-friendly,
deterministic for tests).

Idempotency contract (ISC-6/ISC-10): re-ingest UPSERTs only the ingest-owned
columns. Enrichment columns written by later slices — quality_auto, quality_self,
topic_tags, embedding, consumed, consumed_at — are NEVER touched here, so
re-ingesting never clobbers an embedding or a score.

Anti (ISC-7): this module imports nothing from recall.it / Hopswiki and makes no
such calls. It only reads Readwise and writes the local consume.db.
"""
from __future__ import annotations

import json
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
    }


def mark_listened_podcasts(conn) -> int:
    """A podcast in Readwise is one Jelle has listened to (Jelle, 2026-10-24: "When
    a podcast is in Readwise, I already listened to it"). Mark it consumed, and the
    same episode from the OPML feeds too, matched by title, so the advice never
    offers it as new. Only ever sets consumed=1. Returns the rows changed."""
    n = conn.execute(
        "UPDATE items SET consumed = 1, consumed_at = COALESCE(consumed_at, added_at) "
        "WHERE source = 'readwise' AND item_type = 'podcast' AND consumed = 0").rowcount
    n += conn.execute(
        "UPDATE items SET consumed = 1, consumed_at = ("
        "  SELECT min(r.added_at) FROM items r WHERE r.source = 'readwise' "
        "  AND r.item_type = 'podcast' AND lower(trim(r.title)) = lower(trim(items.title))) "
        "WHERE source = 'podcast-opml' AND consumed = 0 AND lower(trim(title)) IN ("
        "  SELECT lower(trim(title)) FROM items WHERE source = 'readwise' AND item_type = 'podcast')"
    ).rowcount
    return n


def upsert_items(conn, docs: list[dict]) -> tuple[int, int]:
    """Insert new items, refresh ingest-owned fields on existing ones.

    Returns (rows_seen, rows_new). Enrichment columns are never written here,
    except `consumed` on podcasts: see `mark_listened_podcasts`.
    """
    now = datetime.now(timezone.utc).isoformat()
    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    all_cols = ["id"] + _INGEST_COLS
    placeholders = ", ".join(["?"] * len(all_cols))
    update_set = ", ".join(f"{c}=excluded.{c}" for c in _INGEST_COLS)
    sql = (
        f"INSERT INTO items ({', '.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set}"
    )
    seen = 0
    for doc in docs:
        if not doc.get("id"):
            continue
        row = _row_from_doc(doc, now)
        conn.execute(sql, [row[c] for c in all_cols])
        seen += 1
    mark_listened_podcasts(conn)
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


@call_parse
def main(
    location: str = "new",   # Reader location to ingest when fetching live
    from_json: str = "",     # ingest a cached rw-list JSON dump instead of a live fetch
    limit: int = 0,          # cap docs ingested (0 = all)
    db: str = "",            # consume.db path override
):
    "Ingest Readwise documents into consume.db `items` (idempotent upsert)."
    conn = connect(db or None)
    init_schema(conn)
    docs = _load_docs(location, from_json, limit)
    seen, new = upsert_items(conn, docs)
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    src = f"cache {from_json}" if from_json else f"live location={location}"
    print(f"ingested from {src}: {seen} docs seen, {new} new, {total} total in items")
