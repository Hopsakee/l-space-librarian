"""Books and articles: the rule, the migration from `read`, pages, and the book cap."""
import zipfile
import zlib

import pytest

from consume_selection import consume_list as cl
from consume_selection.toread import pdf_pages, text_words

WPP = cl.WORDS_PER_PAGE
ITEMS = [  # id, source, item_type, word_count
    ("lt-1", "librarything", "book", 300 * WPP),
    ("tr-epub", "toread", "epub", 40_000),
    ("tr-long", "toread", "pdf", 26 * WPP),
    ("tr-short", "toread", "pdf", 25 * WPP),
    ("rw-1", "readwise", "pdf", 400 * WPP),
    ("rw-2", "readwise", "article", 2000),
]


@pytest.fixture
def conn(db):
    db.executemany("INSERT INTO items (id, source, title, item_type, word_count, ingested_at) "
                   "VALUES (?, ?, ?, ?, ?, 'x')", [(i, s, f"Title {i}", t, w) for i, s, t, w in ITEMS])
    return db


def test_book_rule(conn):
    media = dict(conn.execute("SELECT item_id, media FROM item_media").fetchall())
    assert media == {"lt-1": "book", "tr-epub": "book", "tr-long": "book",
                     "tr-short": "article", "rw-1": "article", "rw-2": "article"}


def test_read_rows_split_into_books_and_articles(conn):
    old = (cl.CONSUME_ITEMS_DDL.replace("'book','article','watch','listen'", "'read','watch','listen'")
           .replace("    pages_total      INTEGER,            -- books: from the catalogue, editable\n", "")
           .replace("    page_current     INTEGER,            -- books: the page he is on\n", ""))
    conn.execute("DROP TABLE consume_items")
    conn.execute(old)
    rows = [("lt-1", "A book", "read", None), (None, "Imported book", "read", "boek"),
            ("rw-2", "An article", "read", "readwise"), (None, "A note", "read", "hoggle"),
            (None, "A video", "watch", "youtube")]
    conn.executemany("INSERT INTO consume_items (item_id, title, media_type, source, updated_at) "
                     "VALUES (?, ?, ?, ?, 'x')", rows)
    cl.init_consume_schema(conn)
    cl.init_consume_schema(conn)  # a second run changes nothing
    got = {r["title"]: (r["media_type"], r["pages_total"])
           for r in conn.execute("SELECT * FROM consume_items")}
    assert got == {"A book": ("book", 300), "Imported book": ("book", None),
                   "An article": ("article", None), "A note": ("article", None),
                   "A video": ("watch", None)}


def test_add_book_page_and_cap(conn):
    iid = cl.add_book(conn, "lt-1", "2026-Q4")
    assert cl.add_book(conn, "lt-1", "2026-Q4") == iid  # no second row
    with pytest.raises(ValueError):
        cl.add_book(conn, "rw-2", "2026-Q4")  # an article is not a book
    cl.set_page(conn, iid, 40)
    row = cl.get(conn, iid)
    assert (row["status"], row["page_current"], row["pages_total"]) == ("doing", 40, 300)
    with pytest.raises(ValueError):
        cl.set_page(conn, iid, 301)
    w = cl.wip(conn)
    assert (w["open"], w["books_open"], w["book_cap"]) == (0, 1, cl.BOOK_CAP)
    assert [r["id"] for r in cl.search_books(conn, "Title")] == ["tr-epub", "tr-long"]


def test_pdf_pages_from_the_page_tree(tmp_path):
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Pages /Kids [] /Count 30 >> endobj\n"
                      b"2 0 obj << /Count 3 /Type /Pages /Parent 1 0 R >> endobj\n")
    packed = tmp_path / "packed.pdf"
    body = zlib.compress(b"<< /Type /Pages /Count 120 /Kids [] >>")
    packed.write_bytes(b"%PDF-1.5\n5 0 obj << /Type /ObjStm /N 1 /Filter /FlateDecode >>\nstream\n"
                       + body + b"\nendstream\nendobj\n")
    assert (pdf_pages(plain), pdf_pages(packed)) == (30, 120)


def test_epub_words(tmp_path):
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("ch1.xhtml", "<html><body><p>een twee drie</p></body></html>")
        z.writestr("style.css", "p { color: red }")
    assert text_words(p) == 3


# --- work reading ----------------------------------------------------------

def test_work_needs_a_deadline_and_runs_on_it(conn):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO consume_items (title, media_type, updated_at) "
                     "VALUES ('x', 'work', 'x')")
    with pytest.raises(ValueError):
        cl.add_work(conn, "Nota", "volgende week")
    with pytest.raises(ValueError):
        cl.add_work(conn, "Nota", "2026-10-30", url="javascript:alert(1)")
    a = cl.add_work(conn, "", "2026-10-20", item_id="tr-long")      # from ToRead: title, pages
    b = cl.add_work(conn, "Beleidsnota", "2026-10-28", pages=40, url="https://example.org/n")
    row = cl.get(conn, a)
    assert (row["title"], row["pages_total"], row["deadline"]) == ("Title tr-long", 26, "2026-10-20")
    assert [r["id"] for r in cl.list_items(conn, "1999-Q1", "work")] == [a, b]  # no quarter
    w = cl.wip(conn, __import__("datetime").date(2026, 10, 24))
    assert (w["work_late"], w["work_soon"], w["open"]) == (1, 1, 0)
    cl.set_deadline(conn, a, "2026-11-03")
    cl.set_page(conn, a, 5)
    assert (cl.get(conn, a)["deadline"], cl.get(conn, a)["status"]) == ("2026-11-03", "doing")
    assert cl.stale_open(conn, "2026-Q4", "work") == []


def test_old_table_gains_work_and_deadline(conn):
    old = (cl.CONSUME_ITEMS_DDL.replace(",'work'", "")
           .replace("    deadline         TEXT,               -- work: the day it must be read (YYYY-MM-DD)\n", "")
           .replace(",\n    CHECK (media_type <> 'work' OR deadline IS NOT NULL)", ""))
    conn.execute("DROP TABLE consume_items")
    conn.execute(old)
    conn.execute("INSERT INTO consume_items (title, media_type, page_current, updated_at) "
                 "VALUES ('A book', 'book', 12, 'x')")
    cl.init_consume_schema(conn)
    cl.add_work(conn, "Nota", "2026-10-30")
    assert [(r["title"], r["media_type"], r["page_current"]) for r in
            conn.execute("SELECT * FROM consume_items ORDER BY id")] == \
        [("A book", "book", 12), ("Nota", "work", None)]


def test_toread_tree_follows_the_folders(db):
    root = "/home/hoggle/Drive/ToRead/"
    db.executemany(
        "INSERT INTO items (id, source, title, item_type, word_count, location, ingested_at) "
        "VALUES (?, 'toread', ?, 'pdf', 5000, ?, 'x')",
        [("a", "Top", root + "Top.pdf"), ("b", "Nota", root + "Projecten/2026-Q4/Nota.pdf"),
         ("c", "Plan", root + "Projecten/2026-Q4/Plan.md"), ("d", "Gone", "GONE: " + root + "G.pdf")])
    tree = cl.toread_tree(cl.toread_files(db))
    assert [f["id"] for f in tree["files"]] == ["a"]
    q4 = tree["dirs"]["Projecten"]["dirs"]["2026-Q4"]
    assert sorted(f["id"] for f in q4["files"]) == ["b", "c"]
    assert "d" not in [f["id"] for f in tree["files"]] + [f["id"] for f in q4["files"]]
