"""A podcast in Readwise counts as heard, and so does the same feed episode."""
import pytest

from consume_selection.ingest import upsert_items
from consume_selection.podcasts import mark_heard_in_readwise


@pytest.fixture
def conn(db):
    rows = [("pod-1", "podcast-opml", "Ep. 9 with Simon", "podcast", "2026-05-01"),
            ("pod-2", "podcast-opml", "An episode not heard", "podcast", "2026-05-02")]
    db.executemany("INSERT INTO items (id, source, title, item_type, added_at, ingested_at) "
                   "VALUES (?,?,?,?,?, 'x')", rows)
    return db


def consumed(conn):
    return {r["id"]: (r["consumed"], r["consumed_at"])
            for r in conn.execute("SELECT id, consumed, consumed_at FROM items")}


DOCS = [{"id": "rw-1", "title": " ep. 9 WITH Simon", "category": "podcast", "created_at": "2026-05-05"},
        {"id": "rw-2", "title": "An article", "category": "article", "created_at": "2026-05-06"}]


def test_readwise_ingest_marks_podcasts_only(conn):
    upsert_items(conn, DOCS)
    c = consumed(conn)
    assert c["rw-1"] == (1, "2026-05-05")
    assert c["rw-2"] == (0, None)


def test_article_keeps_its_read_state_on_reingest(conn):
    upsert_items(conn, DOCS)
    conn.execute("UPDATE items SET consumed = 1, consumed_at = 'x' WHERE id = 'rw-2'")
    upsert_items(conn, DOCS)
    assert consumed(conn)["rw-2"] == (1, "x")


def test_feed_episode_in_readwise_is_heard_once(conn):
    upsert_items(conn, DOCS)
    assert mark_heard_in_readwise(conn) == 1
    c = consumed(conn)
    assert c["pod-1"] == (1, "2026-05-05")
    assert c["pod-2"][0] == 0
    assert mark_heard_in_readwise(conn) == 0
