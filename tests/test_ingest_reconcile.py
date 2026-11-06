"""Reader ingest keeps `location` truthful and stores reading progress.

A document archived or deleted in Reader stops appearing in the new/later fetch.
After a COMPLETE fetch of both, its row is marked `GONE: <last location>`; after an
incomplete one, nothing is marked.
"""
import json
import sqlite3

import pytest

from consume_selection.db import init_schema
from consume_selection.ingest import main, mark_gone, upsert_items


def doc(id_, location, progress=0.0):
    return {"id": id_, "title": f"Doc {id_}", "category": "article",
            "location": location, "reading_progress": progress, "created_at": "2026-10-01"}


@pytest.fixture
def dbfile(tmp_path):
    """A file db holding four live readwise rows, one toread row and one podcast row."""
    path = tmp_path / "l-space.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    init_schema(c)
    upsert_items(c, [doc("keep-new", "new"), doc("keep-later", "later", 0.8),
                     doc("archived", "later", 1.0), doc("deleted", "new")])
    c.execute("INSERT INTO items (id, source, location, ingested_at) "
              "VALUES ('tr-1', 'toread', '/ToRead/a.pdf', 'x')")
    c.commit()
    c.close()
    return path


def dump(tmp_path, name, docs):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(docs))
    return p


def rows(path):
    c = sqlite3.connect(path)
    out = {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, location, reading_progress FROM items")}
    c.close()
    return out


@pytest.fixture
def todays_reader(tmp_path):
    """What Reader returns now: 'archived' and 'deleted' are gone, a new doc arrived."""
    new = dump(tmp_path, "new", [doc("keep-new", "new"), doc("fresh", "new")])
    later = dump(tmp_path, "later", [doc("keep-later", "later", 0.85)])
    return new, later


def test_full_fetch_marks_unseen_rows_gone(dbfile, todays_reader, capsys):
    new, later = todays_reader
    main(from_json=f"later={later},new={new}", db=str(dbfile), reconcile=True)
    r = rows(dbfile)
    assert r["archived"][0] == "GONE: later"
    assert r["deleted"][0] == "GONE: new"
    assert r["keep-new"][0] == "new" and r["keep-later"][0] == "later"
    assert r["fresh"][0] == "new"
    assert r["tr-1"][0] == "/ToRead/a.pdf"          # other sources untouched
    assert len(r) == 6                              # nothing deleted
    assert "reconcile marked 2 rows GONE" in capsys.readouterr().out


def test_gone_rows_match_the_existing_filter(dbfile, todays_reader):
    new, later = todays_reader
    main(from_json=f"later={later},new={new}", db=str(dbfile), reconcile=True)
    c = sqlite3.connect(dbfile)
    live = {r[0] for r in c.execute(
        "SELECT id FROM items WHERE source = 'readwise' AND location NOT LIKE 'GONE:%'")}
    assert live == {"keep-new", "keep-later", "fresh"}


def test_reappearing_doc_gets_its_location_back(dbfile, todays_reader, tmp_path):
    new, later = todays_reader
    main(from_json=f"later={later},new={new}", db=str(dbfile), reconcile=True)
    back = dump(tmp_path, "later2", [doc("keep-later", "later"), doc("archived", "later", 1.0)])
    main(from_json=f"later={back},new={new}", db=str(dbfile), reconcile=True)
    assert rows(dbfile)["archived"][0] == "later"


def test_gone_mark_is_not_stacked(db):
    upsert_items(db, [doc("a", "later")])
    assert mark_gone(db, set()) == 1
    assert mark_gone(db, set()) == 0
    assert db.execute("SELECT location FROM items").fetchone()[0] == "GONE: later"


def test_failed_fetch_marks_nothing(dbfile, todays_reader, tmp_path):
    _, later = todays_reader
    missing = tmp_path / "no-such-dump.json"
    with pytest.raises(SystemExit) as e:
        main(from_json=f"later={later},new={missing}", db=str(dbfile), reconcile=True)
    assert e.value.code == 1
    r = rows(dbfile)
    assert r["archived"][0] == "later" and r["deleted"][0] == "new"
    assert r["keep-later"][1] == 0.85               # the fetch that worked is still ingested


def test_limited_fetch_marks_nothing(dbfile, todays_reader, capsys):
    new, later = todays_reader
    main(from_json=f"later={later},new={new}", db=str(dbfile), reconcile=True, limit=1)
    r = rows(dbfile)
    assert r["archived"][0] == "later" and r["deleted"][0] == "new"
    assert "reconcile skipped: --limit 1" in capsys.readouterr().out


def test_one_location_only_marks_nothing(dbfile, todays_reader, capsys):
    _, later = todays_reader
    main(from_json=f"later={later}", db=str(dbfile), reconcile=True)
    assert rows(dbfile)["deleted"][0] == "new"
    assert "no complete fetch of new" in capsys.readouterr().out


def test_unlabelled_dump_marks_nothing(dbfile, todays_reader):
    new, later = todays_reader
    main(from_json=f"{later},{new}", db=str(dbfile), reconcile=True)
    assert rows(dbfile)["archived"][0] == "later"


def test_empty_fetch_marks_nothing(dbfile, tmp_path):
    empty = dump(tmp_path, "empty", [])
    main(from_json=f"later={empty},new={empty}", db=str(dbfile), reconcile=True)
    assert rows(dbfile)["keep-later"][0] == "later"


def test_without_reconcile_flag_nothing_is_marked(dbfile, todays_reader):
    new, later = todays_reader
    main(from_json=f"later={later},new={new}", db=str(dbfile))
    assert rows(dbfile)["archived"][0] == "later"


def test_reading_progress_is_written_and_refreshed(db):
    upsert_items(db, [doc("a", "later", 0.8)])
    assert db.execute("SELECT reading_progress FROM items WHERE id='a'").fetchone()[0] == 0.8
    upsert_items(db, [doc("a", "later", 1.0)])
    assert db.execute("SELECT reading_progress FROM items WHERE id='a'").fetchone()[0] == 1.0


def test_old_db_gains_the_column_in_place(tmp_path):
    """The live db predates the column: init_schema must ALTER it in, keeping rows."""
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    init_schema(c)
    c.execute("ALTER TABLE items DROP COLUMN reading_progress")
    c.execute("INSERT INTO items (id, source, location, ingested_at) VALUES ('old', 'readwise', 'later', 'x')")
    c.commit()
    init_schema(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(items)")}
    assert "reading_progress" in cols
    assert c.execute("SELECT id, reading_progress FROM items").fetchone() == ("old", None)
