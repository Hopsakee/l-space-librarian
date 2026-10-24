"""The consume list's writes, and the import's mapping from note frontmatter."""
import datetime
import json

import pytest

from consume_selection import consume_list as cl
from consume_selection.consume_import import parse_frontmatter, row_from_note


@pytest.fixture
def conn(db):
    c = db
    c.execute("INSERT INTO consume_items (title, media_type, quarters, updated_at) "
              "VALUES ('A talk', 'listen', '[\"2026-Q3\"]', 'x')")
    c.commit()
    return c


def test_done_stamps_and_clears_date_finished(conn):
    cl.set_status(conn, 1, "done")
    assert cl.get(conn, 1)["date_finished"]
    cl.set_status(conn, 1, "doing")
    assert cl.get(conn, 1)["date_finished"] is None


def test_drop_needs_a_known_reason(conn):
    with pytest.raises(ValueError):
        cl.drop(conn, 1, "geaccepteerd")
    assert cl.get(conn, 1)["status"] == "todo"
    cl.drop(conn, 1, "iets-urgenter", " te lang ")
    r = cl.get(conn, 1)
    assert (r["status"], r["drop_code"], r["drop_text"]) == ("dropped", "iets-urgenter", "te lang")
    cl.set_status(conn, 1, "todo")
    assert cl.get(conn, 1)["drop_code"] is None


def test_rating_validates_and_clears(conn):
    cl.set_my_rating(conn, 1, "a")
    assert cl.get(conn, 1)["my_rating"] == "A"
    with pytest.raises(ValueError):
        cl.set_my_rating(conn, 1, "E")
    cl.set_my_rating(conn, 1, "")
    assert cl.get(conn, 1)["my_rating"] is None


def test_carry_keeps_history_and_moves_out_of_stale(conn):
    assert [r["id"] for r in cl.stale_open(conn, "2026-Q4", "listen")] == [1]
    cl.carry(conn, 1, "2026-Q4")
    cl.carry(conn, 1, "2026-Q4")
    assert json.loads(cl.get(conn, 1)["quarters"]) == ["2026-Q3", "2026-Q4"]
    assert cl.stale_open(conn, "2026-Q4", "listen") == []
    assert [r["id"] for r in cl.list_items(conn, "2026-Q4", "listen")] == [1]


def test_unknown_item_is_an_error(conn):
    with pytest.raises(ValueError):
        cl.set_stage(conn, 99, "consume")


def test_snipd_link_must_be_snipd(conn):
    with pytest.raises(ValueError):
        cl.set_snipd_url(conn, 1, "https://example.com/x")
    cl.set_snipd_url(conn, 1, "https://share.snipd.com/episode/7a8f408a")
    assert cl.get(conn, 1)["snipd_url"].endswith("7a8f408a")


def test_wip_cap_has_a_floor(conn):
    w = cl.wip(conn)
    assert (w["open"], w["cap"], w["over_cap"]) == (1, cl.WIP_MIN_CAP, False)


def test_wip_splits_this_quarter_from_earlier(conn):
    today = datetime.date(2026, 10, 24)
    assert cl.wip(conn, today)["this_quarter"] == 0
    cl.carry(conn, 1, "2026-Q4")
    w = cl.wip(conn, today)
    assert (w["open"], w["this_quarter"]) == (1, 1)


def test_shift_quarter_crosses_years():
    assert cl.shift_quarter("2026-Q4", 1) == "2027-Q1"
    assert cl.shift_quarter("2026-Q1", -1) == "2025-Q4"


NOTE = """---
title: "Some essay"
processing_stage: {stage}
note-status: {status}
media-type: read
quarter_active:
  - "[[2026-Q2]]"
  - 2026-Q3
location: https://read.readwise.io/read/01abc
rang: 2
goals: ["[[Goal - G2 AI]]"]
my_rating: b
advies-feedback: {fb}
---
body
"""


def _row(conn, **kw):
    fm = parse_frontmatter(NOTE.format(**{"stage": "consume", "status": "🌱todo", "fb": "", **kw}))
    return row_from_note("x/Some essay.md", fm, conn)


def test_import_maps_a_note(conn):
    r = _row(conn)
    assert (r["title"], r["media_type"], r["stage"], r["status"]) == ("Some essay", "read", "consume", "todo")
    assert json.loads(r["quarters"]) == ["2026-Q2", "2026-Q3"]
    assert r["reader_url"] == "https://read.readwise.io/read/01abc" and r["url"] is None
    assert (r["rang"], r["my_rating"]) == (2, "B")
    assert json.loads(r["goals"]) == ["Goal - G2 AI"]


def test_import_blank_stage_is_dropped_with_reason(conn):
    r = _row(conn, stage="", fb="niet-meer-relevant")
    assert (r["status"], r["stage"], r["drop_code"]) == ("dropped", None, "niet-meer-relevant")


def test_import_skips_template_notes(conn):
    fm = parse_frontmatter("---\nprocessing_stage:\nnote-status: 🌱todo\n---\n")
    assert row_from_note("Readwise/x.md", fm, conn) is None
