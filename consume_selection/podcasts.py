"""cs-podcasts — podcast subscriptions (OPML) → consume.db.

Jelle's podcast subscriptions are exported from Snipd as an OPML file. This adapter
folds the most-recent episodes of each subscribed feed into l-space.db so podcasts
join the firehose alongside books, articles and videos, and a listen-scoped query
("best podcasts I can now listen to?") can rank them.

A subscription is ALREADY Jelle's own taste pre-filter, so this adapter does NO LLM
rating and NO transcript fetch — unrated episodes ride the same "unrated → entry
from fit" path as unread books in the depth rules. (Snipd→Readwise highlight capture
is a separate, post-consumption flow and is untouched.)

Duration: `itunes:duration` → the nullable `items.duration_minutes` column (added by
a guarded ALTER in db.init_schema). This is the honest fix for the "duration unknown"
gap in the time-budgeted now-query: real minutes when the feed provides them, NULL
(→ "duration unknown", never fabricated) when it doesn't.

SECURITY (ISC-67): the OPML is a SECRET-BEARING file — one subscription is a private
feed whose URL embeds Jelle's email + a personal access token. The file is read IN
PLACE from ~/Drive/HoggleTransport and is NEVER copied into this repo, a test fixture,
a code comment, or any published doc. Logs print a feed's TITLE + host only, never a
full feed URL. Tests use synthetic OPML fixtures only.

Read-only against every feed (ISC-40/65): HTTP GET only, per-feed timeout, and any
unreachable/malformed feed is logged and skipped — it never aborts the batch.
"""
from __future__ import annotations

import glob
import hashlib
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema
from consume_selection.ingest import upsert_row

# --- constants --------------------------------------------------------------

TRANSPORT = Path.home() / "Drive" / "HoggleTransport"
OPML_GLOB = "snipd_opml_export_*.opml"
ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
FETCH_TIMEOUT = 20          # seconds per feed
FETCH_WORKERS = 8           # parallel feed fetches
UA = "l-space-librarian/0.1 (personal podcast indexer; read-only)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def newest_opml(explicit: str = "") -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise SystemExit(f"OPML not found: {p}")
        return p
    matches = sorted(glob.glob(str(TRANSPORT / OPML_GLOB)))
    if not matches:
        raise SystemExit(f"no OPML export found matching {TRANSPORT / OPML_GLOB}")
    return Path(matches[-1])


# --- OPML + feed parsing ----------------------------------------------------

def parse_opml(path: Path) -> tuple[list[dict], int]:
    """Return ([{title, feed_url}], skipped) — only outlines with a valid http(s)
    xmlUrl. A non-URL xmlUrl (the 'Snipd Announcements' meta entry) is skipped."""
    root = ET.parse(path).getroot()
    feeds: list[dict] = []
    skipped = 0
    for o in root.findall(".//outline"):
        url = o.attrib.get("xmlUrl")
        title = o.attrib.get("title") or o.attrib.get("text") or ""
        if not url or not url.startswith(("http://", "https://")):
            if url is not None:  # a container outline (no xmlUrl) is not "junk"
                skipped += 1
                print(f"skip non-URL feed entry: title={title!r}", file=sys.stderr)
            continue
        feeds.append({"title": title, "feed_url": url})
    return feeds, skipped


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or "?"
    except Exception:
        return "?"


def parse_duration_minutes(raw: str | None) -> int | None:
    """itunes:duration → whole minutes. Accepts 'SS', 'MM:SS', 'HH:MM:SS', or a bare
    seconds integer. Returns None (→ 'duration unknown') on anything unparseable —
    never a fabricated number."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        if ":" in raw:
            parts = [int(p) for p in raw.split(":")]
            while len(parts) < 3:
                parts.insert(0, 0)
            h, m, s = parts[-3], parts[-2], parts[-1]
            total = h * 3600 + m * 60 + s
        else:
            total = int(float(raw))
    except (ValueError, TypeError):
        return None
    if total <= 0:
        return None
    return max(1, round(total / 60))


def _text(el, tag: str) -> str | None:
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else None


def fetch_feed(feed: dict, limit: int) -> tuple[str, list[dict] | None, str]:
    """Fetch + parse ONE feed. Returns (title, episodes|None, status). Never raises —
    a failure returns (title, None, '<error>'), logged by the caller. NO full URL in
    any message (the feed URL may carry a secret token)."""
    url = feed["feed_url"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read()
        root = ET.fromstring(raw)
    except Exception as exc:
        return feed["title"], None, f"{type(exc).__name__}"
    channel = root.find("channel")
    if channel is None:
        return feed["title"], None, "no-channel"
    pod_title = _text(channel, "title") or feed["title"]
    episodes: list[dict] = []
    for item in channel.findall("item")[:limit]:   # RSS items are newest-first
        guid = _text(item, "guid") or _text(item, "link") or _text(item, "title")
        if not guid:
            continue
        ep_title = _text(item, "title") or "(untitled episode)"
        desc = _text(item, "description") or _text(item, f"{ITUNES_NS}summary") or ""
        dur = parse_duration_minutes(_text(item, f"{ITUNES_NS}duration"))
        episodes.append({
            "id": "pod-" + hashlib.sha1((url + guid).encode("utf-8")).hexdigest()[:16],
            "title": ep_title,
            "author": pod_title,
            "url": _text(item, "link"),
            "summary": desc[:2000],
            "added_at": _text(item, "pubDate"),
            "duration_minutes": dur,
            "site_name": pod_title,
        })
    return feed["title"], episodes, "ok"


def upsert_episode(conn, ep: dict) -> None:
    """Idempotent upsert of ONE episode. Keyed on pod-<sha1(feed_url+guid)>; a
    re-run over unchanged feeds writes 0 new rows. Enrichment cols untouched."""
    row = {
        "id": ep["id"],
        "source": "podcast-opml",
        "title": ep["title"],
        "author": ep["author"],
        "url": ep["url"],
        "source_url": ep["url"],
        "site_name": ep["site_name"],
        "summary": ep["summary"],
        "item_type": "podcast",
        "word_count": None,
        "location": "podcast",
        "added_at": ep["added_at"],
        "ingested_at": _now(),
        "duration_minutes": ep["duration_minutes"],
    }
    upsert_row(conn, row, extra_cols=("duration_minutes",))


# --- CLI --------------------------------------------------------------------

@call_parse
def main(
    opml: str = "",         # explicit OPML path (default: newest in ~/Drive/HoggleTransport)
    limit: int = 10,        # most-recent episodes per feed
    dry_run: bool = False,  # fetch + parse + report, write nothing
    db: str = "",           # consume.db path override (tests use a temp db)
):
    "Ingest the most-recent episodes of every subscribed podcast (from a Snipd OPML export) into consume.db. No rating, no transcript."
    path = newest_opml(opml)
    feeds, skipped = parse_opml(path)

    conn = connect(db or None)
    init_schema(conn)

    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    ok_feeds = failed_feeds = episodes = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futs = {pool.submit(fetch_feed, f, limit): f for f in feeds}
        for fut in as_completed(futs):
            title, eps, status = fut.result()
            if eps is None:
                failed_feeds += 1
                print(f"feed failed: title={title!r} host={_host(futs[fut]['feed_url'])} "
                      f"reason={status}", file=sys.stderr)
                continue
            ok_feeds += 1
            for ep in eps:
                if not dry_run:
                    upsert_episode(conn, ep)
                episodes += 1

    if not dry_run:
        conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_pod = conn.execute(
        "SELECT COUNT(*) FROM items WHERE source='podcast-opml'").fetchone()[0]
    with_dur = conn.execute(
        "SELECT COUNT(*) FROM items WHERE source='podcast-opml' AND duration_minutes IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    print(f"podcasts opml={path.name} feeds={len(feeds)} skipped_junk={skipped} "
          f"ok_feeds={ok_feeds} failed_feeds={failed_feeds} episodes={episodes} "
          f"new_rows={after - before} dry_run={dry_run}")
    print(f"podcast rows now {total_pod} ({with_dur} with duration)")
    if not dry_run:
        print("reminder: run `cs-fts --rebuild` so BM25 sees the new episodes")
