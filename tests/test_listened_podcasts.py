"""A podcast in Readwise counts as listened, and so does the same feed episode."""
import sqlite3

import pytest

from consume_selection.db import init_schema
from consume_selection.ingest import mark_listened_podcasts, upsert_items


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    rows = [("pod-1", "podcast-opml", "Ep. 9 with Simon", "podcast", "2026-05-01"),
            ("pod-2", "podcast-opml", "An episode not heard", "podcast", "2026-05-02"),
            ("art-1", "readwise", "An article", "article", "2026-05-03")]
    c.executemany("INSERT INTO items (id, source, title, item_type, added_at, ingested_at) "
                  "VALUES (?,?,?,?,?, 'x')", rows)
    return c


def consumed(conn):
    return {r["id"]: (r["consumed"], r["consumed_at"])
            for r in conn.execute("SELECT id, consumed, consumed_at FROM items")}


def test_readwise_podcast_marks_itself_and_the_feed_episode(conn):
    upsert_items(conn, [{"id": "rw-1", "title": " ep. 9 WITH Simon", "category": "podcast",
                         "created_at": "2026-05-05"}])
    c = consumed(conn)
    assert c["rw-1"] == (1, "2026-05-05")
    assert c["pod-1"] == (1, "2026-05-05")
    assert c["pod-2"][0] == 0 and c["art-1"][0] == 0


def test_never_unmarks_and_is_idempotent(conn):
    conn.execute("UPDATE items SET consumed = 1, consumed_at = 'x' WHERE id = 'pod-2'")
    upsert_items(conn, [{"id": "rw-1", "title": "Ep. 9 with Simon", "category": "podcast",
                         "created_at": "2026-05-05"}])
    assert mark_listened_podcasts(conn) == 0
    assert consumed(conn)["pod-2"] == (1, "x")
