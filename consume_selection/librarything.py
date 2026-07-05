"""cs-librarything — LibraryThing book export → consume.db.

Jelle's LibraryThing library is a JSON export (`librarything_Hopsakee_<ts>.json`,
a dict keyed by books_id). This adapter folds it into l-space.db so books sit in
the SAME catalogue as Readwise articles and YouTube videos — the whole point of
the firehose: book-vs-article-vs-video tradeoffs become possible.

Three-way collection handling (ISC-3.2) — a book is classified by its collections:

  * KNOWN-CANDIDATE ('To read', 'Wishlist')  → an unread book Jelle WANTS to read.
    Upserted `item_type='book'`, `source='librarything'`, `consumed=0` — a real
    candidate the advice layer can surface. Ownership is irrelevant: "if I want to
    read it, I just buy it" (interview round 1). A book that is ALSO in a Read
    collection is a re-read and is treated as READ (dropped as a candidate).

  * KNOWN-READ ('Read', 'Read but unowned')  → a book Jelle has ALREADY read.
    Ingested as a KNOWLEDGE row: `consumed=1` (+ `consumed_at` from entrydate) so it
    NEVER appears as a candidate (candidates filter `consumed=0`) while "what do I
    already know" becomes queryable.

  * KNOWN-IGNORED ('Your library', 'pBook', 'eBook', 'aBook')  → format/ownership
    facets with no reading-intent signal. A book ONLY in these (owned, not read,
    not to-read) is not ingested — ownership alone is not a reason to recommend.

  * ANYTHING ELSE  → LOGGED to stderr (never silently dropped), so a missing
    "not finished" category (the negative signal that does NOT exist in the
    2026-05-17 export) is picked up loudly when a fresh export carries it.

Star ratings (ISC-3.1): Jelle's own 400 star ratings land in `ratings` as
`rater='ik'` (his ground-truth taste). Stars → tier per STAR_TIER. Upserted
ON CONFLICT(item_id, rater) mirroring watchlater.write_rating, so re-runs never
duplicate rows.

Read-only against LibraryThing: this reads a local JSON export and writes only the
local consume.db. Nothing is ever written back to any account (ISC-40). BOOKS.md /
Books.md are never touched (ISC-41).
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema
from consume_selection.ingest import _INGEST_COLS

# --- constants --------------------------------------------------------------

DOWNLOADS = Path.home() / "Drive" / "Downloads"
EXPORT_GLOB = "librarything_Hopsakee_*.json"

# Collection taxonomy (ISC-3.2). Everything outside these three sets is LOGGED.
KNOWN_CANDIDATE = {"To read", "Wishlist"}
KNOWN_READ = {"Read", "Read but unowned"}
KNOWN_IGNORED = {"Your library", "pBook", "eBook", "aBook"}
KNOWN = KNOWN_CANDIDATE | KNOWN_READ | KNOWN_IGNORED

# Stars → quality tier (interview round 1; half-stars exist in the export: 3.5, 4.5).
#   4.5-5 → s | 4 → a | 3-3.5 → b | 2-2.5 → c | ≤1.5 → d
def star_tier(rating) -> str | None:
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    if r >= 4.5:
        return "s"
    if r >= 4.0:
        return "a"
    if r >= 3.0:
        return "b"
    if r >= 2.0:
        return "c"
    return "d"


# Words-per-page heuristic for a book's word_count (used by the depth rules'
# BOOK_CLASS >20k-word test; a rough estimate is fine, never displayed as fact).
WORDS_PER_PAGE = 275

RATER = "ik"  # Jelle's OWN judgment — the ground-truth rater


# --- helpers ----------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def newest_export(explicit: str = "") -> Path:
    """Resolve the export path: explicit arg > newest matching download."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise SystemExit(f"librarything export not found: {p}")
        return p
    matches = sorted(glob.glob(str(DOWNLOADS / EXPORT_GLOB)))
    if not matches:
        raise SystemExit(
            f"no LibraryThing export found matching {DOWNLOADS / EXPORT_GLOB}"
        )
    return Path(matches[-1])  # lexicographic sort works: the ts suffix is zero-padded


def classify(collections: list[str]) -> str:
    """Classify a record by its collections: 'read' | 'candidate' | 'ignored'.

    READ wins over CANDIDATE (a book in both To-read and Read is a re-read →
    treated as read, dropped as a candidate — ISC accepted, documented)."""
    cols = set(collections or [])
    if cols & KNOWN_READ:
        return "read"
    if cols & KNOWN_CANDIDATE:
        return "candidate"
    return "ignored"


def build_summary(rec: dict) -> str:
    """Fold title/author/genre/subjects/summary into one BM25-usable text (ISC-5)."""
    parts: list[str] = []
    if rec.get("summary"):
        parts.append(str(rec["summary"]))
    genre = rec.get("genre") or []
    if genre:
        parts.append("Genre: " + ", ".join(str(g) for g in genre))
    # subject is a list of single-element lists in the export.
    subjects: list[str] = []
    for s in (rec.get("subject") or []):
        if isinstance(s, list):
            subjects.extend(str(x) for x in s)
        else:
            subjects.append(str(s))
    if subjects:
        parts.append("Subjects: " + "; ".join(subjects))
    return "\n".join(parts).strip()


def word_count(rec: dict) -> int | None:
    pages = str(rec.get("pages") or "").strip()
    try:
        n = int(float(pages))
    except (TypeError, ValueError):
        return None
    return n * WORDS_PER_PAGE if n > 0 else None


def consumed_at(rec: dict) -> str | None:
    """entrydate (YYYY-MM-DD) as the read-completion proxy for a Read book."""
    d = str(rec.get("entrydate") or "").strip()
    return d or None


def row_from_record(rec: dict, kind: str) -> dict:
    """Map a LibraryThing record + its class to an items-table row dict."""
    return {
        "id": f"lt-{rec['books_id']}",
        "source": "librarything",
        "title": rec.get("title"),
        "author": rec.get("primaryauthor"),
        "url": f"https://www.librarything.com/work/{rec.get('workcode')}"
        if rec.get("workcode") else None,
        "source_url": None,
        "site_name": "librarything",
        "summary": build_summary(rec) or None,
        "item_type": "book",
        "word_count": word_count(rec),
        "location": "librarything",
        "added_at": consumed_at(rec),
        "ingested_at": _now(),
    }


def upsert_item(conn, row: dict, *, consumed: int, consumed_at_val: str | None) -> None:
    """Idempotent upsert of ONE book into `items`. Refreshes the ingest-owned
    columns (via _INGEST_COLS, shared with watchlater/ingest so they never drift)
    PLUS the read-state pair (consumed/consumed_at) which this adapter owns. Never
    touches enrichment columns (embedding, quality_auto, topic_tags)."""
    cols = ["id"] + _INGEST_COLS + ["consumed", "consumed_at"]
    row = {**row, "consumed": consumed, "consumed_at": consumed_at_val}
    placeholders = ", ".join(["?"] * len(cols))
    update_set = ", ".join(f"{c}=excluded.{c}" for c in _INGEST_COLS + ["consumed", "consumed_at"])
    conn.execute(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set}",
        [row[c] for c in cols],
    )


def write_rating(conn, item_id: str, tier: str, stars) -> None:
    """Idempotent upsert of Jelle's own star rating into `ratings`, keyed on
    (item_id, rater='ik'). Mirrors watchlater.write_rating: tier lowercased,
    raw_tag records the source stars. Re-runs never duplicate (composite PK)."""
    t = tier.lower()
    conn.execute(
        "INSERT INTO ratings (item_id, tier, rater, raw_tag) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(item_id, rater) DO UPDATE SET "
        "tier=excluded.tier, raw_tag=excluded.raw_tag",
        [item_id, t, RATER, f"librarything/{stars}star"],
    )


# --- CLI --------------------------------------------------------------------

@call_parse
def main(
    export: str = "",       # explicit export path (default: newest in ~/Drive/Downloads)
    dry_run: bool = False,  # print planned counts, write nothing
    db: str = "",           # consume.db path override (tests use a temp db)
):
    "Ingest a LibraryThing JSON export into consume.db: To-read/Wishlist as candidates, Read as knowledge, star ratings as rater='ik'."
    path = newest_export(export)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"unexpected export shape (want dict of records): {type(data)}")

    conn = connect(db or None)
    init_schema(conn)

    candidates = read = ignored = rated = 0
    unknown: dict[str, int] = {}

    for rec in data.values():
        if not rec.get("books_id"):
            continue
        cols = rec.get("collections") or []
        # Log any collection outside the known taxonomy (never silently drop signal).
        for c in cols:
            if c not in KNOWN:
                unknown[c] = unknown.get(c, 0) + 1

        kind = classify(cols)
        if kind == "ignored":
            ignored += 1
            continue

        row = row_from_record(rec, kind)
        is_read = kind == "read"
        if not dry_run:
            upsert_item(conn, row, consumed=1 if is_read else 0,
                        consumed_at_val=consumed_at(rec) if is_read else None)

        if is_read:
            read += 1
        else:
            candidates += 1

        # Star rating → ratings(rater='ik'), for any ingested book that carries one.
        tier = star_tier(rec.get("rating"))
        if tier:
            if not dry_run:
                write_rating(conn, row["id"], tier, rec.get("rating"))
            rated += 1

    if not dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    ik = conn.execute("SELECT COUNT(*) FROM ratings WHERE rater='ik'").fetchone()[0]
    conn.close()

    print(f"librarything export={path.name} candidates={candidates} read={read} "
          f"ignored={ignored} rated={rated} dry_run={dry_run}")
    print(f"items total now {total}; ratings(rater='ik') now {ik}")
    if unknown:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(unknown.items()))
        print(f"UNKNOWN collections (logged, not dropped): {summary}", file=sys.stderr)
    if not dry_run:
        print("reminder: run `cs-fts --rebuild` so BM25 sees the new book rows")
