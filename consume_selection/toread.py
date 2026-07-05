"""cs-toread — the ~/Drive/ToRead filesystem folder → consume.db.

ToRead is Jelle's OWN curated pile of PDFs/EPUBs/notes he saved to read later —
often his highest-relevance material for a topic (his own reports, source packs).
l-space.db never indexed it, so _READING_ADVICE documented a manual `find` gotcha.
This adapter closes that: the pile becomes queryable in the same catalogue.

It ships as a DRAIN, not a faucet (SystemsThinking): every row carries its
quarter-folder age, so the advice/report layer can surface "still unread after N
quarters" — indexing dead stock comes with an exit path (purge-or-commit), not just
more pile.

Idempotency (ISC-11): keyed on `toread-<sha1(relpath)[:16]>`, so a re-scan of an
unchanged tree inserts 0 new rows.

Delisting (ISC-12): a file REMOVED from ToRead must stop being recommended, but a
single missing scan (an unmounted Drive, a temporary move) must NOT wrongly delist.
So a row is delisted only after **2 CONSECUTIVE misses**, tracked in a sidecar JSON
next to the db (no schema change). On delist the row is marked `consumed=1` (drops
out of candidates, which filter consumed=0) and its `location` gets a `GONE:` prefix
for transparency — the row is NEVER deleted (its ratings/links stay valid).

Read-only against the filesystem (ISC-40): reads file paths, writes only consume.db
+ the sidecar. Never moves/deletes/edits a ToRead file.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastcore.script import call_parse

from consume_selection.db import connect, db_path, init_schema
from consume_selection.ingest import upsert_row

# --- constants --------------------------------------------------------------

TOREAD_ROOT = Path.home() / "Drive" / "ToRead"
DOC_EXTS = {".pdf", ".epub", ".md", ".html"}

# A path is skipped if ANY of its parts matches one of these (case-insensitive).
# Covers the ISC-10 set (sync.ffs_db, Keybindings/!Keybindings, _read, hidden) plus
# the build-note additions (Zotero, Stories).
_SKIP_EXACT = {"sync.ffs_db", "_read", "zotero", "stories"}
_SKIP_SUBSTR = ("keybindings",)  # matches both 'Keybindings' and '!Keybindings'

# Quarter folder in a path, BOTH separator forms exist in the tree (2025_Q3, 2026-Q2).
_QUARTER_RE = re.compile(r"(\d{4})[_-]Q([1-4])", re.I)
# A trailing Obsidian/export UUID on a filename stem, stripped for a human title.
_UUID_RE = re.compile(
    r"[-_ ]?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

DELIST_MISS_THRESHOLD = 2  # consecutive misses before a vanished file is delisted


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sidecar_path(db_override: str | None) -> Path:
    """Miss-count sidecar next to the db (per-db so tests get their own)."""
    p = db_path(db_override)
    return p.parent / f"{p.stem}-toread-seen.json"


# --- scanning ---------------------------------------------------------------

def is_skipped(rel: Path) -> bool:
    for part in rel.parts:
        low = part.lower()
        if part.startswith("."):          # hidden file/dir
            return True
        if low in _SKIP_EXACT:
            return True
        if any(s in low for s in _SKIP_SUBSTR):
            return True
    return False


def scan(root: Path = TOREAD_ROOT) -> list[Path]:
    """Recursive scan for doc files, minus the excluded paths. Returns relpaths."""
    if not root.exists():
        raise SystemExit(f"ToRead root not found: {root}")
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in DOC_EXTS:
            continue
        rel = p.relative_to(root)
        if is_skipped(rel):
            continue
        out.append(rel)
    return sorted(out)


def item_id(rel: Path) -> str:
    return "toread-" + hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:16]


def clean_title(rel: Path) -> str:
    """A human title from a filename — never a raw path (ISC-13). Strips a trailing
    export UUID, turns underscores into spaces, collapses whitespace, drops leading
    punctuation. No PDF metadata read (no cheap lib available; filename is enough)."""
    stem = rel.stem
    stem = _UUID_RE.sub("", stem)
    stem = stem.replace("_", " ")
    stem = re.sub(r"\s+", " ", stem)
    stem = stem.lstrip(" ;,.-").strip()
    return stem or rel.stem or "Untitled ToRead document"


def quarter_of(rel: Path) -> str | None:
    """The quarter folder in the path (normalised `YYYY-Q<N>`), or None."""
    m = _QUARTER_RE.search(str(rel))
    return f"{m.group(1)}-Q{m.group(2).upper()}" if m else None


def build_summary(rel: Path, title: str) -> str:
    q = quarter_of(rel)
    if q:
        return f"ToRead document: {title}. In the pile since {q}."
    return f"ToRead document: {title}."


def row_from_file(rel: Path, root: Path) -> dict:
    title = clean_title(rel)
    return {
        "id": item_id(rel),
        "source": "toread",
        "title": title,
        "author": None,
        "url": None,
        "source_url": None,
        "site_name": "toread",
        "summary": build_summary(rel, title),
        "item_type": rel.suffix.lower().lstrip("."),  # pdf|epub|md|html
        "word_count": None,
        "location": str(root / rel),  # absolute path (ISC-9)
        "added_at": None,
        "ingested_at": _now(),
    }


def upsert_file(conn, row: dict) -> None:
    """Idempotent upsert of ONE ToRead file. Re-appearing after a delist clears the
    GONE state: consumed→0 so it can be recommended again (shared `ingest.upsert_row`)."""
    upsert_row(conn, {**row, "consumed": 0, "consumed_at": None},
               extra_cols=("consumed", "consumed_at"))


# --- CLI --------------------------------------------------------------------

@call_parse
def main(
    root: str = "",         # ToRead root override (default ~/Drive/ToRead)
    dry_run: bool = False,  # scan + report only, write nothing
    db: str = "",           # consume.db path override (tests use a temp db)
):
    "Scan ~/Drive/ToRead for pdf/epub/md/html, upsert into consume.db, and delist files that vanished for 2 consecutive scans."
    scan_root = Path(root).expanduser() if root else TOREAD_ROOT
    files = scan(scan_root)
    current = {item_id(rel): rel for rel in files}

    conn = connect(db or None)
    init_schema(conn)

    # 1. upsert everything present now
    if not dry_run:
        for rel in files:
            upsert_file(conn, row_from_file(rel, scan_root))
        conn.commit()

    # 2. delist logic: any toread row in the db NOT present now is a miss.
    side = sidecar_path(db or None)
    seen: dict = json.loads(side.read_text()) if side.exists() else {}
    db_toread = {
        r["id"]: {"location": r["location"], "consumed": r["consumed"]}
        for r in conn.execute(
            "SELECT id, location, consumed FROM items WHERE source='toread'")
    }
    delisted = pending = 0
    new_seen: dict = {}
    for iid, meta in db_toread.items():
        if iid in current:
            new_seen[iid] = {"relpath": str(current[iid]), "miss": 0}
            continue
        prev = seen.get(iid, {})
        miss = int(prev.get("miss", 0)) + 1
        new_seen[iid] = {"relpath": prev.get("relpath", ""), "miss": miss}
        loc = meta["location"] or ""
        already_gone = loc.startswith("GONE:")
        if miss >= DELIST_MISS_THRESHOLD and not already_gone:
            if not dry_run:
                conn.execute(
                    "UPDATE items SET consumed=1, location=? WHERE id=?",
                    (f"GONE: {loc}", iid),
                )
            delisted += 1
        elif miss < DELIST_MISS_THRESHOLD and not already_gone:
            pending += 1

    if not dry_run:
        conn.commit()
        side.write_text(json.dumps(new_seen, indent=0))
    total_toread = conn.execute(
        "SELECT COUNT(*) FROM items WHERE source='toread'").fetchone()[0]
    conn.close()

    print(f"toread root={scan_root} scanned={len(files)} in_db={len(db_toread)} "
          f"delisted={delisted} delist_pending={pending} dry_run={dry_run}")
    print(f"toread rows now {total_toread}")
    if not dry_run:
        print("reminder: run `cs-fts --rebuild` so BM25 sees the new/updated rows")
