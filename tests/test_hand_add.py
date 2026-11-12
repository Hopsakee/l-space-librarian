"""Articles, videos and podcasts added by hand from the catalogue (2026-11-12),
and the advice item that makes way when that puts the list over the cap."""
import pytest

from consume_selection import consume_list as cl

Q = "2026-Q4"
ITEMS = [  # id, source, item_type, title, author, url, source_url, location
    ("yt-1", "youtube-wl", "video", "Dams explained", "Practical Engineering",
     "https://www.youtube.com/watch?v=yt-1", None, "watchlater"),
    ("yt-gone", "youtube-wl", "video", "Dams removed", "Practical Engineering",
     "https://www.youtube.com/watch?v=x", None, "GONE:2026-11-01"),
    ("pod-1", "podcast-opml", "podcast", "Groundwater episode", "Hydro Show",
     "https://example.org/ep1", None, "podcast"),
    ("rw-1", "readwise", "article", "Polder pumps", "a blog",
     "https://read.readwise.io/read/rw-1", "https://blog.example/pumps", "later"),
    ("hw-1", "hopswiki", "article", "Polder synthesis", None, None, None,
     "/home/hoggle/Obsidian/Hopswiki/Hopswiki/syntheses/polder.md"),
]


@pytest.fixture
def conn(db):
    db.executemany("INSERT INTO items (id, source, item_type, title, author, url, source_url, "
                   "location, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x')", ITEMS)
    db.executemany("INSERT INTO ratings (item_id, tier, rater, raw_tag) VALUES (?, ?, ?, '')",
                   [("yt-1", "c", "claude-haiku-yt"), ("yt-1", "a", "claude-sonnet-yt"),
                    ("yt-1", "s", "ik")])
    return db


def advice(conn, n, media="watch", start=0):
    """n open advice items (they carry a lane), oldest first."""
    for k in range(start, start + n):
        conn.execute("INSERT INTO consume_items (title, media_type, status, quarters, lane, "
                     "date_added, updated_at) VALUES (?, ?, 'todo', ?, 'werk', ?, 'x')",
                     (f"advice {k}", media, f'["{Q}"]', f"2026-10-{10 + k:02d}"))
    conn.commit()


def test_search_by_medium_skips_gone_and_open(conn):
    assert [r["id"] for r in cl.search_items(conn, "watch", "dams")] == ["yt-1"]
    assert [r["id"] for r in cl.search_items(conn, "listen", "hydro")] == ["pod-1"]
    assert {r["id"] for r in cl.search_items(conn, "article", "polder")} == {"rw-1", "hw-1"}
    assert cl.search_items(conn, "watch", "dams")[0]["ai_tier"] == "A"  # Sonnet, not 'ik'
    cl.add_item(conn, "yt-1", "watch", Q)
    assert cl.search_items(conn, "watch", "dams") == []
    with pytest.raises(ValueError):
        cl.search_items(conn, "book", "x")


def test_add_sets_links_source_and_no_lane(conn):
    rows = {i: cl.get(conn, cl.add_item(conn, i, m, Q)[0])
            for i, m in [("yt-1", "watch"), ("pod-1", "listen"), ("rw-1", "article"),
                         ("hw-1", "article")]}
    assert (rows["yt-1"]["source"], rows["yt-1"]["url"], rows["yt-1"]["auto_rating"]) == \
        ("youtube", "https://www.youtube.com/watch?v=yt-1", "A")
    assert rows["pod-1"]["source"] == "podcast"
    assert (rows["rw-1"]["reader_url"], rows["rw-1"]["url"]) == \
        ("https://read.readwise.io/read/rw-1", "https://blog.example/pumps")
    assert rows["hw-1"]["file_name"] == "Hopswiki: Hopswiki/syntheses/polder.md"
    assert all(r["lane"] is None and r["quarters"] == f'["{Q}"]' for r in rows.values())


def test_wrong_medium_or_readd(conn):
    with pytest.raises(ValueError):
        cl.add_item(conn, "yt-1", "listen", Q)
    iid, _ = cl.add_item(conn, "yt-1", "watch", Q)
    cl.drop(conn, iid, "geen-energie-of-tijd")
    again, _ = cl.add_item(conn, "yt-1", "watch", "2027-Q1")
    row = cl.get(conn, again)
    assert again == iid and row["status"] == "todo" and "2027-Q1" in row["quarters"]


def test_under_cap_nothing_makes_way(conn):
    advice(conn, cl.WIP_MIN_CAP - 1)
    _, gone = cl.add_item(conn, "yt-1", "watch", Q)
    assert gone is None
    assert cl.wip(conn)["open"] == cl.WIP_MIN_CAP


def test_over_cap_oldest_advice_of_same_medium_makes_way(conn):
    advice(conn, 2, media="article")                # older, but another medium
    advice(conn, cl.WIP_MIN_CAP - 2, media="watch", start=2)
    conn.execute("UPDATE consume_items SET status = 'doing' WHERE title = 'advice 2'")  # started
    conn.commit()
    iid, gone = cl.add_item(conn, "yt-1", "watch", Q)
    assert gone["title"] == "advice 3"
    assert (gone["status"], gone["drop_code"], gone["drop_text"]) == \
        ("dropped", "iets-urgenter", "plaats gemaakt voor: Dams explained")
    assert cl.wip(conn)["open"] == cl.WIP_MIN_CAP
    assert cl.get(conn, iid)["status"] == "todo"


def test_over_cap_falls_back_to_other_medium_never_hand_or_book(conn):
    advice(conn, cl.WIP_MIN_CAP, media="article")
    conn.execute("INSERT INTO consume_items (title, media_type, status, quarters, updated_at) "
                 "VALUES ('my own', 'watch', 'todo', '[]', 'x')")  # no lane: hand-added
    conn.commit()
    _, gone = cl.add_item(conn, "yt-1", "watch", Q)
    assert gone["title"] == "advice 0" and gone["media_type"] == "article"


def test_over_cap_with_nothing_to_drop_still_adds(conn):
    for k in range(cl.WIP_MIN_CAP):
        conn.execute("INSERT INTO consume_items (title, media_type, status, quarters, "
                     "updated_at) VALUES (?, 'watch', 'todo', '[]', 'x')", (f"own {k}",))
    conn.commit()
    iid, gone = cl.add_item(conn, "yt-1", "watch", Q)
    assert gone is None and cl.get(conn, iid)["status"] == "todo"
    assert cl.wip(conn)["over_cap"]


def test_refresh_request_lifecycle(conn):
    import datetime as dt
    t0 = dt.datetime(2026, 11, 12, 10, 0, tzinfo=dt.timezone.utc)
    st = cl.refresh_state(conn, t0)
    assert (st["pending"], st["done_at"], st["fresh"]) == (False, None, False)
    assert cl.request_refresh(conn, now=t0)
    assert not cl.request_refresh(conn, now=t0 + dt.timedelta(minutes=1))   # one is waiting
    assert cl.refresh_state(conn, t0 + dt.timedelta(minutes=1))["pending"]
    assert cl.refresh_state(conn, t0 + dt.timedelta(minutes=6))["stuck"]   # box not running
    # the box claims and finishes it
    conn.execute("UPDATE catalogue_refresh SET started_at = ?, done_at = ?, ok = 1, "
                 "result = '3 nieuwe video''s'", ((t0 + dt.timedelta(minutes=1)).isoformat(),) * 2)
    conn.commit()
    t2 = t0 + dt.timedelta(minutes=2)
    st = cl.refresh_state(conn, t2)
    assert (st["pending"], st["fresh"], st["ok"], st["result"]) == (False, True, True, "3 nieuwe video's")
    assert not cl.request_refresh(conn, now=t2)                 # just refreshed: no new one
    assert cl.request_refresh(conn, force=True, now=t2)         # the button forces one
    assert not cl.request_refresh(conn, now=t2 + dt.timedelta(minutes=1))  # still waiting
    assert cl.request_refresh(conn, now=t0 + dt.timedelta(minutes=30))  # stuck: ask again
