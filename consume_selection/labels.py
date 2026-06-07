"""cs-label — record per-query relevance feedback (the bake-off ground-truth).

Usage emerges from the shortlist flow: the generator returns ~10 items for a
focused knowledge question; you mark each relevant or not. Those marks accumulate
in `query_labels`, keyed by (query, item). Ground-truth is a BYPRODUCT of use —
never an upfront blocking step. The retrieval bake-off (precision@10) reads these.

    cs-label --query "how do embeddings handle dynamic interests?" --item 01ab... --relevant true
    cs-label --query "..." --item 01ab... --relevant false
    cs-label --query "..." --show          # list current labels for a query
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema

_TRUE = {"1", "true", "t", "yes", "y", "relevant", "ja"}
_FALSE = {"0", "false", "f", "no", "n", "irrelevant", "nee"}


def parse_bool(s: str) -> int:
    v = (s or "").strip().lower()
    if v in _TRUE:
        return 1
    if v in _FALSE:
        return 0
    raise SystemExit(f"--relevant must be true/false (got {s!r})")


def ensure_query(conn, text: str) -> int:
    """Return the id of the query with this text, inserting it if new."""
    text = text.strip()
    if not text:
        raise SystemExit("--query text is required")
    row = conn.execute("SELECT id FROM queries WHERE text=?", (text,)).fetchone()
    if row:
        return row["id"]
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("INSERT INTO queries (text, created_at) VALUES (?,?)", (text, now))
    conn.commit()
    return cur.lastrowid


def set_label(conn, query_id: int, item_id: str, relevant: int) -> None:
    """Upsert one (query, item) relevance label."""
    if not conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        raise SystemExit(f"item {item_id!r} not in items — ingest it first")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO query_labels (query_id, item_id, relevant, labeled_at) VALUES (?,?,?,?) "
        "ON CONFLICT(query_id, item_id) DO UPDATE SET relevant=excluded.relevant, labeled_at=excluded.labeled_at",
        (query_id, item_id, relevant, now),
    )
    conn.commit()


@call_parse
def main(
    query: str = "",       # the focused knowledge question the shortlist answered
    item: str = "",        # Readwise item id to label
    relevant: str = "",    # true / false (yes/no, ja/nee, 1/0)
    show: bool = False,    # list existing labels for --query instead of writing
    db: str = "",
):
    "Record or show per-query relevance labels (relevant true/false)."
    conn = connect(db or None)
    init_schema(conn)
    qid = ensure_query(conn, query)
    if show:
        rows = conn.execute(
            "SELECT ql.relevant, ql.item_id, i.title FROM query_labels ql "
            "JOIN items i ON i.id=ql.item_id WHERE ql.query_id=? ORDER BY ql.relevant DESC",
            (qid,),
        ).fetchall()
        print(f"query #{qid}: {query!r} — {len(rows)} label(s)")
        for r in rows:
            mark = "✓" if r["relevant"] else "✗"
            print(f"  {mark} {r['item_id']}  {(r['title'] or '')[:60]}")
        conn.close()
        return
    if not item or not relevant:
        raise SystemExit("provide --item and --relevant (or --show)")
    set_label(conn, qid, item, parse_bool(relevant))
    conn.close()
    print(f"labeled query #{qid} item {item} relevant={parse_bool(relevant)}")
