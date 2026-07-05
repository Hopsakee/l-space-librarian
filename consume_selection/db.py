"""consume.db schema + connection helpers.

The consume-index is a single-file SQLite read-model. Two tables in this slice:

  items   — one row per consumable item (a Readwise document, for now).
            Carries the columns the ISA's ISC-1/ISC-3 mandate plus useful
            provenance. quality_self / quality_auto / topic_tags / embedding
            are populated by LATER slices (scoring + embeddings); created here
            so no migration is needed when those land.

  ratings — every `_rating/<tier>[/<rater>]` tag found on an item, one row per
            (item_id, rater). This is the F8 ground-truth substrate. Built
            GENERICALLY because the ISA's assumed `rating/<tier>/zelf` tag does
            not exist in the real data (see ISA Decisions 2026-06-07).

Default location ~/code_data/l-space-librarian/l-space.db — "L-space" is the
library-space of Unseen University (Terry Pratchett's Discworld); this project is
the librarian that collects consumable items into it. Sibling to
~/code_data/inconceivable/. Override with --db or the CONSUME_DB env var.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastcore.script import call_parse

DEFAULT_DB = Path.home() / "code_data" / "l-space-librarian" / "l-space.db"

# --- schema -----------------------------------------------------------------

ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,   -- Readwise document id (UNIQUE => idempotent upsert)
    source        TEXT NOT NULL,      -- firehose source, e.g. 'readwise'
    title         TEXT,
    author        TEXT,
    url           TEXT,               -- reader url
    source_url    TEXT,               -- original document url
    site_name     TEXT,
    summary       TEXT,               -- Readwise-generated summary (no LLM call here)
    item_type     TEXT,               -- Readwise category: article|email|video|pdf|...
    word_count    INTEGER,
    location      TEXT,               -- new|later|shortlist|archive|feed
    added_at      TEXT,               -- Readwise created_at
    ingested_at   TEXT NOT NULL,      -- when this row was written/refreshed
    -- populated by later slices; present now to avoid a migration --
    quality_auto  TEXT,               -- LLM tier S/A/B/C/D (model-rated)
    quality_self  TEXT,               -- Jelle's own tier (ground-truth)
    topic_tags    TEXT,               -- JSON array of topic tags
    embedding     BLOB,               -- summary embedding (vector bytes)
    consumed      INTEGER NOT NULL DEFAULT 0,  -- read-state (F11; flow built later)
    consumed_at   TEXT,
    duration_minutes INTEGER          -- playback minutes for time-bearing items (podcasts/videos)
);
"""

# Columns added AFTER the first ITEMS_DDL shipped. init_schema runs a guarded
# ALTER for each so an existing db (which CREATE TABLE IF NOT EXISTS won't touch)
# migrates in place — migration-safe, no flag day. Fresh dbs get them from the DDL.
_MIGRATION_COLUMNS = {
    "duration_minutes": "INTEGER",   # F-L: itunes:duration for podcasts; benefits videos later
}

# Every rating tag found on an item. rater = model name (e.g. 'mistrall-small-4'),
# the sentinel 'bare' for an un-attributed `_rating/<tier>` tag, or 'ik' for
# Jelle's OWN judgment (`_rating/<tier>/ik`) — the ground-truth rater.
RATINGS_DDL = """
CREATE TABLE IF NOT EXISTS ratings (
    item_id   TEXT NOT NULL REFERENCES items(id),
    tier      TEXT NOT NULL,          -- s|a|b|c|d|undefined (lowercased)
    rater     TEXT NOT NULL,          -- model name, 'bare', or 'ik' (Jelle's own)
    raw_tag   TEXT NOT NULL,          -- original tag name, case preserved
    PRIMARY KEY (item_id, rater)
);
"""

# A focused knowledge question the shortlist generator was asked. Normalised so
# labels key on a short stable id instead of the (long, volatile) question text.
QUERIES_DDL = """
CREATE TABLE IF NOT EXISTS queries (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);
"""

# Per-query relevance feedback: when a shortlist returns items for a query, Jelle
# marks each relevant true/false. This is the bake-off ground-truth, harvested as
# a BYPRODUCT of use — never an upfront blocking step. precision@10 is computed
# per query from these rows.
QUERY_LABELS_DDL = """
CREATE TABLE IF NOT EXISTS query_labels (
    query_id    INTEGER NOT NULL REFERENCES queries(id),
    item_id     TEXT NOT NULL REFERENCES items(id),
    relevant    INTEGER NOT NULL,     -- 1 = relevant, 0 = not relevant
    labeled_at  TEXT NOT NULL,
    PRIMARY KEY (query_id, item_id)
);
"""

INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_items_location ON items(location);",
    "CREATE INDEX IF NOT EXISTS idx_items_type ON items(item_type);",
    "CREATE INDEX IF NOT EXISTS idx_ratings_rater ON ratings(rater);",
    "CREATE INDEX IF NOT EXISTS idx_ratings_tier ON ratings(tier);",
    "CREATE INDEX IF NOT EXISTS idx_query_labels_item ON query_labels(item_id);",
]


def db_path(override: str | None = None) -> Path:
    """Resolve the consume.db path: arg > CONSUME_DB env > default."""
    p = override or os.environ.get("CONSUME_DB")
    return Path(p).expanduser() if p else DEFAULT_DB


def connect(override: str | None = None) -> sqlite3.Connection:
    """Open (creating parent dir if needed) the consume.db with FK + row factory."""
    path = db_path(override)
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30: the unattended nightly jobs (watchlater 03:30, readwise 04:00)
    # can overlap on this file; the default 5s lock wait is too short for a bulk
    # write and turns contention into a hard "database is locked" crash.
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")  # per-connection; safe
    # NOTE: journal_mode=WAL is deliberately NOT set here. WAL is persistent in
    # the db header (not per-connection); forcing it from library code would
    # silently rewrite the shared live db and interacts with how backups copy
    # the file. The live db is already WAL; changing that is an architecture
    # decision for the backup strategy, not a library default. (audit 2026-07-02)
    return conn


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """Guarded ALTER TABLE for columns added after the first ITEMS_DDL shipped.
    Idempotent: only adds a column absent from the live table."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, decl in _MIGRATION_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl};")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables + indexes if absent, migrate late-added columns. Idempotent."""
    conn.execute(ITEMS_DDL)
    conn.execute(RATINGS_DDL)
    conn.execute(QUERIES_DDL)
    conn.execute(QUERY_LABELS_DDL)
    _migrate_columns(conn)
    for ddl in INDEXES_DDL:
        conn.execute(ddl)
    conn.commit()


@call_parse
def main(db: str = ""):
    "Create the consume.db schema (items + ratings). Idempotent."
    conn = connect(db or None)
    init_schema(conn)
    path = db_path(db or None)
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()
    print(f"consume.db ready at {path}")
    print(f"tables: {', '.join(tables)}")
