"""cs-fts — FTS5 lexical (BM25) retrieval over consume.db items.

One of the retrieval methods the bake-off compares. Standalone FTS5 table over
title + summary (a small copy of 1.8k rows — cheap). `--rebuild` re-syncs it from
`items`; `--query` runs a BM25 search and prints ranked items (lower rank = better).

FTS5 must be compiled into the Python sqlite3 build (it is on standard Linux
builds); if absent, rebuild() surfaces a clear error rather than silently failing.
"""
from __future__ import annotations

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema

FTS_DDL = "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(item_id UNINDEXED, title, summary);"


def ensure_fts(conn) -> None:
    try:
        conn.execute(FTS_DDL)
    except Exception as e:  # FTS5 not compiled in
        raise SystemExit(f"FTS5 unavailable in this sqlite build: {e}")


def rebuild_fts(conn) -> int:
    """Drop and repopulate the FTS index from items. Returns rows indexed."""
    ensure_fts(conn)
    conn.execute("DELETE FROM items_fts;")
    conn.execute(
        "INSERT INTO items_fts(item_id, title, summary) "
        "SELECT id, COALESCE(title,''), COALESCE(summary,'') FROM items;"
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0]


def _fts_safe_query(query: str) -> str:
    """Turn arbitrary natural-language text into a valid FTS5 MATCH expression.

    Raw user text ("hello AND", 'a "b', "foo OR") is interpreted as FTS5 query
    syntax and raises OperationalError on stray operators/quotes. Tokenising and
    quoting each token as a phrase makes any input a safe implicit-AND term
    search — the sensible default for reading-advice queries.
    """
    import re
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    return " ".join(f'"{t}"' for t in tokens)


def search(conn, query: str, limit: int = 10) -> list[dict]:
    """BM25 search over the FTS index; lower rank = better match.

    The query is sanitised into quoted phrase tokens so that free-text input
    (which is FTS5 syntax) can never crash the search with an OperationalError.
    """
    ensure_fts(conn)
    safe = _fts_safe_query(query)
    if not safe:
        return []
    rows = conn.execute(
        "SELECT f.item_id, i.title, bm25(items_fts) AS rank "
        "FROM items_fts f JOIN items i ON i.id = f.item_id "
        "WHERE items_fts MATCH ? ORDER BY rank LIMIT ?",
        (safe, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@call_parse
def main(
    query: str = "",       # BM25 query (FTS5 syntax); omit with --rebuild
    rebuild: bool = False,  # rebuild the FTS index from items
    limit: int = 10,
    db: str = "",
):
    "Build the FTS5 lexical index, or run a BM25 query against it."
    conn = connect(db or None)
    init_schema(conn)
    if rebuild:
        n = rebuild_fts(conn)
        conn.close()
        print(f"FTS index rebuilt: {n} rows")
        return
    if not query:
        raise SystemExit("provide --query, or --rebuild")
    hits = search(conn, query, limit)
    conn.close()
    print(f"{len(hits)} hit(s) for {query!r}:")
    for i, h in enumerate(hits, 1):
        print(f"  {i}. [{h['rank']:.2f}] {h['item_id']}  {(h['title'] or '')[:60]}")
