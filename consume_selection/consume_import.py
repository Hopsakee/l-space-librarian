"""One-time import of the Braincave consume list into `consume_items`.

Until 2026-10 an item was on the list when its note's frontmatter had a
non-empty `processing_stage`; `remove` blanked that field and kept the note.
So a note is imported when it has a stage, or when it has a `quarter_active`
(it was on a list once, and was removed later: imported as dropped). The ~750
Readwise highlight notes whose template stamps an empty `processing_stage`
have neither, and are skipped.

Never overwrites: a note already imported (by `note_path`) is left alone, so a
second run cannot undo what was changed in the app since.

    python -m consume_selection.consume_import --vault ~/Obsidian/Braincave --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from .consume_list import DROP_CODES, MEDIA, STAGES, TIERS, _now, init_consume_schema

SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".stversions"}
QUARTER_RE = re.compile(r"(\d{4}-Q[1-4])")

# Readwise's export writes `type` as its own category name.
TYPE_TO_MEDIA = {"podcasts": "listen", "articles": "article", "books": "book",
                 "tweets": "article", "supplementals": "article"}

_FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---", re.S)
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")
_LIST_RE = re.compile(r"^\s+-\s+(.*)$")


def parse_frontmatter(text: str) -> dict | None:
    """Scalars and block lists, no YAML library. Same parser as ConsumeList.py."""
    m = _FM_RE.match(text)
    if not m:
        return None
    fm: dict = {}
    key = None
    for ln in m.group(1).split("\n"):
        li = _LIST_RE.match(ln)
        if li and key is not None:
            if not isinstance(fm.get(key), list):
                fm[key] = []
            fm[key].append(li.group(1).strip())
            continue
        kv = _KV_RE.match(ln)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        fm[key] = val
    return fm


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _list(v) -> list[str]:
    """A frontmatter list as clean strings: block list, `[a, b]`, or a scalar."""
    if isinstance(v, list):
        raw = v
    else:
        s = _s(v)
        if not s or s == "[]":
            return []
        raw = s[1:-1].split(",") if s.startswith("[") and not s.startswith("[[") else [s]
    return [x for x in (r.strip().strip("\"'") for r in raw) if x]


def _unlink(s: str) -> str:
    return s.replace("[[", "").replace("]]", "").strip()


def quarters_of(fm: dict) -> list[str]:
    raw = fm.get("quarter_active")
    text = " ".join(raw) if isinstance(raw, list) else _s(raw)
    return sorted(set(QUARTER_RE.findall(text)))


def media_of(fm: dict, links: dict) -> str | None:
    m = _s(fm.get("media-type")).lower()
    by_type = TYPE_TO_MEDIA.get(_s(fm.get("type")).lower())
    if m == "read":  # the vault had one reading medium; a Readwise book is a book
        return by_type if by_type in ("book", "article") else "article"
    if m in MEDIA:
        return m
    if by_type:
        return by_type
    if "youtube" in (links.get("url") or "") or _s(fm.get("source")) == "youtube":
        return "watch"
    return None


def links_of(fm: dict) -> dict:
    """Sort `location` and `url` into the link columns by what they point at."""
    out: dict = {"url": None, "reader_url": None, "file_name": None}
    for raw in (_s(fm.get("location")), _s(fm.get("url"))):
        if not raw:
            continue
        if "read.readwise.io" in raw:
            out["reader_url"] = out["reader_url"] or raw
        elif raw.startswith(("http://", "https://")):
            out["url"] = out["url"] or raw
        else:
            out["file_name"] = out["file_name"] or raw
    return out


def status_of(fm: dict, stage: str) -> str:
    ns = _s(fm.get("note-status")).lower()
    if "done" in ns:
        return "done"
    if "vervallen" in ns or not stage:
        return "dropped"
    return "doing" if "doing" in ns else "todo"


def match_item(conn, links: dict) -> sqlite3.Row | None:
    """The catalogue row for these links: Reader link first, then the original."""
    for col, val in (("url", links["reader_url"]), ("url", links["url"]),
                     ("source_url", links["url"])):
        if val:
            row = conn.execute(f"SELECT id, url, source_url FROM items WHERE {col} = ?",
                               (val,)).fetchone()
            if row:
                return row
    return None


def row_from_note(rel: str, fm: dict, conn) -> dict | None:
    stage = _s(fm.get("processing_stage")).lower()
    quarters = quarters_of(fm)
    if not stage and not quarters:
        return None
    links = links_of(fm)
    item = match_item(conn, links)
    if item:
        if not links["reader_url"] and "read.readwise.io" in (item["url"] or ""):
            links["reader_url"] = item["url"]
        if not links["url"] and (item["source_url"] or "").startswith("http"):
            links["url"] = item["source_url"]
    status = status_of(fm, stage)
    title = _unlink(_s(fm.get("title"))) or Path(rel).stem
    fb = _s(fm.get("advies-feedback"))

    def tier(k):
        t = _s(fm.get(k)).upper()
        return t if t in TIERS else None

    rang = _s(fm.get("rang"))
    return {
        "item_id": item["id"] if item else None,
        "title": title,
        "author": _unlink(_s(fm.get("author"))) or None,
        "media_type": media_of(fm, links),
        "source": _s(fm.get("source")) or None,
        **links,
        "stage": stage if stage in STAGES else None,
        "status": status,
        "revisit": int(_s(fm.get("revisit")).lower() in ("true", "yes", "1")),
        "quarters": json.dumps(quarters),
        "rang": int(rang) if rang.isdigit() else None,
        "lane": _s(fm.get("lane")) or None,
        "goals": json.dumps([_unlink(g) for g in _list(fm.get("goals"))], ensure_ascii=False),
        "projects": json.dumps([_unlink(p) for p in _list(fm.get("projects"))], ensure_ascii=False),
        "landing": _s(fm.get("landing")) or None,
        "auto_rating": tier("auto_rating"),
        "auto_rating_date": _s(fm.get("auto_rating_date")) or None,
        "my_rating": tier("my_rating"),
        "date_added": _s(fm.get("date_added")) or None,
        "date_finished": (_s(fm.get("date_finished")) or None) if status == "done" else None,
        "drop_code": fb if status == "dropped" and fb in DROP_CODES else None,
        "drop_text": (_s(fm.get("advies-toelichting")) or None) if status == "dropped" else None,
        "note_path": rel,
        "updated_at": _now(),
    }


def scan(vault: Path, conn) -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for path in sorted(vault.rglob("*.md")):
        if any(p in SKIP_DIRS for p in path.parts):
            continue
        try:
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not fm or "processing_stage" not in fm:
            continue
        rel = str(path.relative_to(vault))
        row = row_from_note(rel, fm, conn)
        if row is None:
            continue
        if row["media_type"] is None:
            problems.append(f"no media type, skipped: {rel}")
            continue
        rows.append(row)
    return rows, problems


def main(argv=None) -> int:
    from .db import connect, init_schema
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", default=str(Path.home() / "Obsidian" / "Braincave"))
    ap.add_argument("--db", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    conn = connect(a.db or None)
    rows, problems = scan(Path(a.vault).expanduser(), conn)
    print(f"notes to import: {len(rows)}")
    print("by status:", dict(Counter(r["status"] for r in rows)))
    print("by media:", dict(Counter(r["media_type"] for r in rows)))
    print("linked to a catalogue item:", sum(1 for r in rows if r["item_id"]))
    print("open with no link at all:", sum(
        1 for r in rows if r["status"] in ("todo", "doing")
        and not (r["url"] or r["reader_url"] or r["file_name"])))
    for p in problems:
        print("WARN", p)
    if a.dry_run:
        return 0

    init_schema(conn)
    cols = list(rows[0]) if rows else []
    sql = (f"INSERT INTO consume_items ({', '.join(cols)}) "
           f"VALUES ({', '.join(':' + c for c in cols)}) ON CONFLICT(note_path) DO NOTHING")
    inserted = 0
    with conn:
        for r in rows:
            inserted += conn.execute(sql, r).rowcount
    print(f"inserted {inserted}, already present {len(rows) - inserted}")
    print("rows in consume_items:", conn.execute("SELECT count(*) FROM consume_items").fetchone()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
