"""The consume list: what Jelle has DECIDED to read, watch or listen to.

Reading is two media since 2026-10-24: a `book` takes days or weeks and is read
in sessions, with its pages tracked; an `article` (also a document or a
Braincave note) is one sitting. `item_media` decides which a catalogue item is.

Everything else in l-space.db is the firehose (what was collected and how the
models rated it). `consume_items` is the commitment layer on top of it: the
items taken on for a quarter, tracked to done or dropped, with his own rating.

It lives in the same database as `items` and `ratings` on purpose (decided
2026-10-23): tuning the rating prompt needs the model's tier and his verdict
side by side, and that is one join here instead of a sync between two stores.

Until 2026-10 this list lived as frontmatter on Braincave notes
(`processing_stage` as membership marker). `consume_import.py` moves it here.

Stdlib only, so the web app and the box skills can both use it.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3

MEDIA = ("book", "article", "watch", "listen")
STAGES = ("skim", "consume", "highlight", "visualise")
STATUSES = ("todo", "doing", "done", "dropped")
TIERS = ("S", "A", "B", "C", "D")

# Why an item was dropped. Copy of Braincave `Extra/metadata/advies-feedback.md`
# without `geaccepteerd`, which means "followed the advice" and is never a reason
# to drop something.
DROP_CODES = (
    "iets-urgenter",
    "geen-energie-of-tijd",
    "advies-was-fout",
    "interesse-verschoven",
    "schatting-fout",
    "niet-meer-relevant",
)

# The WIP cap: open items against twice what got finished in the last quarter,
# never below the floor. Same formula as _CONSUME_LIST's ConsumeList.py.
# Books have their own fixed cap and do not count here (Jelle, 2026-10-24: one
# for the G8 reading club, one for the ds. Dekker kring, a story, one or two more).
WIP_WINDOW_DAYS = 90
WIP_MIN_CAP = 10
BOOK_CAP = 5

# A ToRead PDF longer than this is a book; shorter is a document (an article).
# Pages are stored as word_count = pages x WORDS_PER_PAGE, as LibraryThing does.
BOOK_MIN_PAGES = 25
WORDS_PER_PAGE = 275

CONSUME_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS consume_items (
    id               INTEGER PRIMARY KEY,
    item_id          TEXT REFERENCES items(id),   -- the catalogue row, when there is one
    title            TEXT NOT NULL,
    author           TEXT,
    media_type       TEXT NOT NULL CHECK (media_type IN ('book','article','watch','listen')),
    source           TEXT,               -- youtube | readwise | podcast | boek | hoggle | ...
    -- where to open it; any of these may be empty
    url              TEXT,               -- the original: YouTube, article, episode page
    reader_url       TEXT,               -- read.readwise.io/read/<id>
    snipd_url        TEXT,               -- share.snipd.com/episode/<id>, pasted by hand
    file_name        TEXT,               -- a local file, shown as copyable text
    stage            TEXT CHECK (stage IN ('skim','consume','highlight','visualise')),
    status           TEXT NOT NULL DEFAULT 'todo'
                     CHECK (status IN ('todo','doing','done','dropped')),
    revisit          INTEGER NOT NULL DEFAULT 0,  -- 1 = already consumed, on the list to revisit
    quarters         TEXT NOT NULL DEFAULT '[]',  -- JSON list of YYYY-QN, CUMULATIVE
    -- why it was advised, as stamped by _READING_ADVICE
    rang             INTEGER,            -- 1 (highest) .. 4
    lane             TEXT,
    goals            TEXT NOT NULL DEFAULT '[]',  -- JSON list
    projects         TEXT NOT NULL DEFAULT '[]',  -- JSON list
    landing          TEXT,               -- what it should feed
    auto_rating      TEXT CHECK (auto_rating IN ('S','A','B','C','D')),
    auto_rating_date TEXT,
    my_rating        TEXT CHECK (my_rating IN ('S','A','B','C','D')),
    date_added       TEXT,
    date_finished    TEXT,
    drop_code        TEXT,
    drop_text        TEXT,
    dropped_at       TEXT,
    note_path        TEXT UNIQUE,        -- the Braincave note it was imported from
    pages_total      INTEGER,            -- books: from the catalogue, editable
    page_current     INTEGER,            -- books: the page he is on
    updated_at       TEXT NOT NULL
);
"""

# Which consume medium a catalogue item is. The one place the book rule lives:
# books come from LibraryThing, or are a ToRead EPUB or a ToRead PDF over
# BOOK_MIN_PAGES; any other text (Readwise, Hopswiki, short PDFs) is an article.
ITEM_MEDIA_DDL = f"""
CREATE VIEW item_media AS
SELECT id AS item_id,
       CASE WHEN item_type = 'video' THEN 'watch'
            WHEN item_type = 'podcast' THEN 'listen'
            WHEN source = 'librarything' THEN 'book'
            WHEN source = 'toread' AND item_type = 'epub' THEN 'book'
            WHEN source = 'toread' AND item_type = 'pdf'
                 AND word_count > {BOOK_MIN_PAGES * WORDS_PER_PAGE} THEN 'book'
            ELSE 'article' END AS media,
       CAST(round(word_count * 1.0 / {WORDS_PER_PAGE}) AS INTEGER) AS pages
FROM items;
"""

CONSUME_INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_consume_status ON consume_items(status);",
    "CREATE INDEX IF NOT EXISTS idx_consume_item ON consume_items(item_id);",
]


def init_consume_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP VIEW IF EXISTS item_media")  # recreated, so a rule change lands
    conn.execute(ITEM_MEDIA_DDL)
    conn.execute(CONSUME_ITEMS_DDL)
    _split_read(conn)
    for ddl in CONSUME_INDEXES_DDL:
        conn.execute(ddl)
    conn.commit()


def _split_read(conn: sqlite3.Connection) -> None:
    """One-time migration of a table made before 2026-10-24, when reading was one
    medium, `read`. SQLite cannot change a CHECK in place, so the table is rebuilt
    and each `read` row becomes a book or an article by `item_media`; a row with
    no catalogue item is an article unless it was imported as source `boek`."""
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'consume_items'").fetchone()[0]
    if "'book'" in ddl:
        return
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(consume_items)")]
    book = ("(o.source = 'boek' OR (SELECT media FROM item_media m "
            "WHERE m.item_id = o.item_id) = 'book')")
    new = {"media_type": f"CASE WHEN o.media_type <> 'read' THEN o.media_type "
                         f"WHEN {book} THEN 'book' ELSE 'article' END",
           "pages_total": "(SELECT pages FROM item_media m WHERE m.item_id = o.item_id "
                          "AND m.media = 'book')"}
    cols = old_cols + [c for c in new if c not in old_cols]
    select = ", ".join(new.get(c, f"o.{c}") for c in cols)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("ALTER TABLE consume_items RENAME TO consume_items_old")
    conn.execute(CONSUME_ITEMS_DDL)
    conn.execute(f"INSERT INTO consume_items ({', '.join(cols)}) "
                 f"SELECT {select} FROM consume_items_old o")
    conn.execute("DROP TABLE consume_items_old")
    conn.commit()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return _dt.date.today().isoformat()


def current_quarter(today: _dt.date | None = None) -> str:
    today = today or _dt.date.today()
    return f"{today.year}-Q{(today.month - 1) // 3 + 1}"


def shift_quarter(q: str, n: int) -> str:
    year, num = int(q[:4]), int(q[-1])
    idx = year * 4 + (num - 1) + n
    return f"{idx // 4}-Q{idx % 4 + 1}"


# --- reading ---------------------------------------------------------------

_IN_QUARTER = "EXISTS (SELECT 1 FROM json_each(quarters) WHERE value = :q)"
_ORDER = "ORDER BY rang IS NULL, rang, date_added, id"


def list_items(conn, quarter: str, media: str, show: str = "open") -> list[sqlite3.Row]:
    """The items of one quarter and one medium. `show` is open | done | dropped."""
    statuses = {"open": ("todo", "doing"), "done": ("done",), "dropped": ("dropped",)}[show]
    marks = ",".join("?" * len(statuses))
    sql = (f"SELECT * FROM consume_items WHERE media_type = ? AND status IN ({marks}) "
           f"AND EXISTS (SELECT 1 FROM json_each(quarters) WHERE value = ?) {_ORDER}")
    return conn.execute(sql, (media, *statuses, quarter)).fetchall()


def counts(conn, quarter: str, show: str = "open") -> dict[str, int]:
    """Items per medium for one quarter, for the tab labels."""
    statuses = {"open": ("todo", "doing"), "done": ("done",), "dropped": ("dropped",)}[show]
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT media_type, count(*) FROM consume_items WHERE status IN ({marks}) "
        f"AND EXISTS (SELECT 1 FROM json_each(quarters) WHERE value = ?) GROUP BY media_type",
        (*statuses, quarter)).fetchall()
    out = dict.fromkeys(MEDIA, 0)
    out.update({m: n for m, n in rows})
    return out


def stale_open(conn, quarter: str, media: str) -> list[sqlite3.Row]:
    """Open items NOT on this quarter: never carried over, or never given one.
    Each needs a decision, carry it or drop it."""
    return conn.execute(
        f"SELECT * FROM consume_items WHERE media_type = :m AND status IN ('todo','doing') "
        f"AND NOT {_IN_QUARTER} {_ORDER}", {"m": media, "q": quarter}).fetchall()


def wip(conn, today: _dt.date | None = None) -> dict:
    """Open items against the cap. A signal, not a gate. The cap counts every
    open item except books; `this_quarter` says how many are on the current
    quarter. Books are counted against their own BOOK_CAP."""
    today = today or _dt.date.today()
    cutoff = (today - _dt.timedelta(days=WIP_WINDOW_DAYS)).isoformat()
    open_ = "status IN ('todo','doing') AND media_type <> 'book'"
    n_open = conn.execute(f"SELECT count(*) FROM consume_items WHERE {open_}").fetchone()[0]
    done = conn.execute(
        "SELECT count(*) FROM consume_items WHERE status = 'done' AND media_type <> 'book' "
        "AND date_finished >= ?", (cutoff,)).fetchone()[0]
    on_q = conn.execute(f"SELECT count(*) FROM consume_items WHERE {open_} AND {_IN_QUARTER}",
                        {"q": current_quarter(today)}).fetchone()[0]
    books = conn.execute("SELECT count(*) FROM consume_items WHERE media_type = 'book' "
                         "AND status IN ('todo','doing')").fetchone()[0]
    cap = max(2 * done, WIP_MIN_CAP)
    return {"open": n_open, "this_quarter": on_q, "cap": cap, "done_in_window": done,
            "over_cap": n_open > cap, "books_open": books, "book_cap": BOOK_CAP,
            "books_over_cap": books > BOOK_CAP}


def get(conn, iid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM consume_items WHERE id = ?", (iid,)).fetchone()


# --- writing ---------------------------------------------------------------
# Every write is one UPDATE of one row, committed at once. Each raises
# ValueError on a value outside its enum, so a bad form post changes nothing.

def _update(conn, iid: int, **cols) -> None:
    cols["updated_at"] = _now()
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    cur = conn.execute(f"UPDATE consume_items SET {sets} WHERE id = :id", {**cols, "id": iid})
    if cur.rowcount != 1:
        raise ValueError(f"no consume item {iid}")
    conn.commit()


def set_status(conn, iid: int, status: str) -> None:
    """todo / doing / done. Done stamps date_finished; leaving done clears it.
    Dropping goes through `drop`, because it needs a reason."""
    if status not in ("todo", "doing", "done"):
        raise ValueError(f"status must be todo, doing or done, not {status!r}")
    _update(conn, iid, status=status,
            date_finished=_today() if status == "done" else None,
            drop_code=None, drop_text=None, dropped_at=None)


def set_stage(conn, iid: int, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}, not {stage!r}")
    _update(conn, iid, stage=stage)


def set_my_rating(conn, iid: int, tier: str) -> None:
    """His own verdict. An empty tier clears it."""
    tier = tier.strip().upper()
    if tier and tier not in TIERS:
        raise ValueError(f"rating must be one of {TIERS}, not {tier!r}")
    _update(conn, iid, my_rating=tier or None)


def drop(conn, iid: int, code: str, text: str = "") -> None:
    """Take an item off the list with a reason. The row stays, for the ledger."""
    if code not in DROP_CODES:
        raise ValueError(f"reason must be one of {DROP_CODES}, not {code!r}")
    _update(conn, iid, status="dropped", drop_code=code,
            drop_text=text.strip() or None, dropped_at=_today(), date_finished=None)


def carry(conn, iid: int, quarter: str) -> None:
    """Put an open item on `quarter` too. History is kept: quarters only grow."""
    row = get(conn, iid)
    if row is None:
        raise ValueError(f"no consume item {iid}")
    quarters = json.loads(row["quarters"] or "[]")
    if quarter not in quarters:
        quarters.append(quarter)
    _update(conn, iid, quarters=json.dumps(sorted(quarters)))


def set_snipd_url(conn, iid: int, url: str) -> None:
    url = url.strip()
    if url and not url.startswith("https://share.snipd.com/"):
        raise ValueError("a Snipd link starts with https://share.snipd.com/")
    _update(conn, iid, snipd_url=url or None)


# --- books -----------------------------------------------------------------

def set_page(conn, iid: int, page: int) -> None:
    """The page he is on. Starting a book moves it from todo to doing."""
    row = get(conn, iid)
    if row is None or row["media_type"] != "book":
        raise ValueError(f"no book {iid}")
    if page < 0 or (row["pages_total"] and page > row["pages_total"]):
        raise ValueError(f"pagina {page} ligt buiten 0–{row['pages_total']}")
    cols = {"page_current": page}
    if row["status"] == "todo" and page > 0:
        cols["status"] = "doing"
    _update(conn, iid, **cols)


def set_pages_total(conn, iid: int, pages: int | None) -> None:
    if pages is not None and pages < 1:
        raise ValueError("aantal pagina's moet minstens 1 zijn")
    _update(conn, iid, pages_total=pages)


def search_books(conn, text: str, limit: int = 15) -> list[sqlite3.Row]:
    """Books he owns (LibraryThing, ToRead) whose title or author contains `text`,
    not already open on the list. Unread first."""
    like = f"%{text.strip()}%"
    return conn.execute(
        "SELECT i.id, i.title, i.author, i.source, i.consumed, m.pages FROM items i "
        "JOIN item_media m ON m.item_id = i.id "
        "WHERE m.media = 'book' AND (i.title LIKE :t OR i.author LIKE :t) "
        "AND (i.location IS NULL OR i.location NOT LIKE 'GONE:%') "
        "AND i.id NOT IN (SELECT item_id FROM consume_items WHERE item_id IS NOT NULL "
        "                 AND status IN ('todo','doing')) "
        "ORDER BY i.consumed, i.title LIMIT :n", {"t": like, "n": limit}).fetchall()


def add_book(conn, item_id: str, quarter: str) -> int:
    """Put a book he owns on the list for `quarter`, by hand: the one thing the
    app may add (Jelle, 2026-10-24: books he picks himself). A book that was on
    the list before comes back to todo on this quarter instead of a second row."""
    item = conn.execute(
        "SELECT i.*, m.pages FROM items i JOIN item_media m ON m.item_id = i.id "
        "WHERE i.id = ? AND m.media = 'book'", (item_id,)).fetchone()
    if item is None:
        raise ValueError(f"geen boek {item_id}")
    old = conn.execute("SELECT id FROM consume_items WHERE item_id = ? ORDER BY id DESC",
                       (item_id,)).fetchone()
    if old:
        carry(conn, old["id"], quarter)
        set_status(conn, old["id"], "todo")
        return old["id"]
    local = item["source"] == "toread"
    cur = conn.execute(
        "INSERT INTO consume_items (item_id, title, author, media_type, source, url, file_name, "
        "status, quarters, revisit, pages_total, date_added, updated_at) "
        "VALUES (?, ?, ?, 'book', 'boek', ?, ?, 'todo', ?, ?, ?, ?, ?)",
        (item_id, item["title"], item["author"], None if local else item["url"],
         item["location"] if local else None, json.dumps([quarter]), item["consumed"],
         item["pages"], _today(), _now()))
    conn.commit()
    return cur.lastrowid
