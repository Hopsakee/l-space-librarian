"""Listen or read: the by_ear flag on articles (2026-11-18)."""
import sqlite3

import pytest

from consume_selection import consume_list as cl

Q = "2026-Q4"


@pytest.fixture
def conn(db):
    db.executemany("INSERT INTO consume_items (title, media_type, quarters, updated_at) "
                   "VALUES (?, 'article', ?, 'x')",
                   [("An essay", f'["{Q}"]'), ("A tutorial with charts", f'["{Q}"]')])
    db.commit()
    return db


def test_flag_starts_unjudged(conn):
    r = cl.get(conn, 1)
    assert (r["by_ear"], r["by_ear_why"], r["by_ear_by"]) == (None, None, None)


def test_set_and_clear(conn):
    cl.set_by_ear(conn, 1, 1, " lopend betoog ", "model")
    r = cl.get(conn, 1)
    assert (r["by_ear"], r["by_ear_why"], r["by_ear_by"]) == (1, "lopend betoog", "model")
    cl.set_by_ear(conn, 1, None)
    r = cl.get(conn, 1)
    assert (r["by_ear"], r["by_ear_why"], r["by_ear_by"]) == (None, None, None)


def test_bad_values_change_nothing(conn):
    with pytest.raises(ValueError):
        cl.set_by_ear(conn, 1, 2)
    with pytest.raises(ValueError):
        cl.set_by_ear(conn, 1, 1, by="someone")
    assert cl.get(conn, 1)["by_ear"] is None


def test_filter_keeps_only_listenable(conn):
    cl.set_by_ear(conn, 1, 1, by="model")
    cl.set_by_ear(conn, 2, 0, by="rules")
    assert [r["title"] for r in cl.list_items(conn, Q, "article", by_ear=True)] == ["An essay"]
    assert len(cl.list_items(conn, Q, "article")) == 2
    conn.execute("UPDATE consume_items SET quarters = '[\"2026-Q2\"]'")
    conn.commit()
    assert [r["title"] for r in cl.stale_open(conn, Q, "article", by_ear=True)] == ["An essay"]
    assert len(cl.stale_open(conn, Q, "article")) == 2


def test_old_table_gains_the_columns_and_keeps_rows():
    """A live table from before 2026-11-18 is migrated in place on first start."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    old_ddl = cl.CONSUME_ITEMS_DDL
    for col in cl.ADDED_COLUMNS:
        old_ddl = "\n".join(l for l in old_ddl.splitlines() if not l.strip().startswith(col))
    c.execute("CREATE TABLE items (id TEXT PRIMARY KEY, source TEXT, item_type TEXT, "
              "word_count INTEGER)")
    c.execute(old_ddl)
    c.execute("INSERT INTO consume_items (title, media_type, quarters, updated_at) "
              "VALUES ('kept', 'article', '[]', 'x')")
    c.commit()
    assert "by_ear" not in {r[1] for r in c.execute("PRAGMA table_info(consume_items)")}
    cl.init_consume_schema(c)
    cl.init_consume_schema(c)  # a second start is a no-op
    cols = {r[1] for r in c.execute("PRAGMA table_info(consume_items)")}
    assert set(cl.ADDED_COLUMNS) <= cols
    assert c.execute("SELECT title FROM consume_items").fetchone()[0] == "kept"
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE consume_items SET by_ear = 5")
