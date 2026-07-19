"""cs-mismatch — record when Jelle contradicts a Watch-Later prune suggestion.

When `cs-watchlater` advises deleting a video (relevant but tier B-or-lower) and Jelle
watches/keeps it anyway, that disagreement is a strong signal a quality rating was wrong.
This module records each contradiction in `wl_mismatch` (keyed on video_id, idempotent) and
computes which channels Jelle has contradicted >= TRUST_THRESHOLD *distinct* times.
`watchlater.py` consults `trusted_channels()` to SUPPRESS those channels from the prune lane —
the video is still ingested + rated, it just stops being suggested for deletion.

Design rules (ISA 20260719-wl-mismatch-channel-trust):
  * Explicit capture only. No passive WL-disappearance inference, no follow/agreement tracking.
  * Trust SUPPRESSES the prune suggestion only; it never re-rates, deletes, or auto-recommends.
  * PK on video_id => re-recording the same video is idempotent and can never inflate the count;
    trust needs TRUST_THRESHOLD *distinct* videos on one channel.
  * Read-only against YouTube — this module never touches yt-dlp or the account.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema

# Distinct contradicted videos on a channel before it is trusted (Jelle, 2026-07-19:
# "if I contradicted your advice three times, just trust the channel").
TRUST_THRESHOLD = 3

WL_MISMATCH_DDL = """
CREATE TABLE IF NOT EXISTS wl_mismatch (
    video_id    TEXT PRIMARY KEY,   -- one row per contradicted video (idempotent upsert)
    channel     TEXT NOT NULL,      -- denormalized channel, resolved at record time
    title       TEXT,
    my_tier     TEXT,               -- the tier I had assigned (from ratings), if any
    decision    TEXT NOT NULL,      -- Jelle's decision, e.g. 'kept' / 'watched'
    note        TEXT,               -- optional free-text why
    recorded_at TEXT NOT NULL       -- ISO-8601 UTC
);
"""

# Matches a bare 11-char id, youtu.be/<id>, or any youtube.com URL carrying v=<id>.
_VID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_wl_mismatch(conn) -> None:
    """Create the wl_mismatch table if absent. Idempotent — safe to call every run."""
    conn.execute(WL_MISMATCH_DDL)


def extract_video_id(video: str) -> str:
    """Accept a bare video id or any common YouTube URL and return the 11-char id."""
    v = (video or "").strip()
    m = _VID_RE.search(v)
    if m:
        return m.group(1)
    # Bare id (already 11 chars, url-safe) — the common case when I resolve from the DB.
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", v):
        return v
    return v  # last resort: store what we were given rather than crash


def _resolve_from_items(conn, video_id: str) -> tuple[str | None, str | None, str | None]:
    """Best-effort (channel, title, tier) for an already-ingested WL video.

    items.id == video_id and items.author == channel for youtube-wl rows; the tier comes
    from `ratings` (prefer the WL rater 'claude-haiku-yt', else any). All three may be None
    for a video that was never ingested — the caller then requires an explicit --channel.
    """
    row = conn.execute(
        "SELECT author, title FROM items WHERE id = ?", [video_id]
    ).fetchone()
    channel = row["author"] if row is not None else None
    title = row["title"] if row is not None else None
    trow = conn.execute(
        "SELECT tier FROM ratings WHERE item_id = ? "
        "ORDER BY (rater = 'claude-haiku-yt') DESC LIMIT 1",
        [video_id],
    ).fetchone()
    tier = trow["tier"] if trow is not None else None
    return channel, title, tier


def record_mismatch(conn, video_id: str, decision: str, note: str = "",
                    channel: str | None = None, title: str | None = None,
                    my_tier: str | None = None) -> dict:
    """Idempotent upsert of ONE contradiction, keyed on video_id. Missing channel/title/tier
    are resolved from the ingested item. Raises ValueError if no channel can be determined."""
    r_channel, r_title, r_tier = _resolve_from_items(conn, video_id)
    channel = channel or r_channel
    title = title or r_title
    my_tier = my_tier or r_tier
    if not channel:
        raise ValueError(
            f"no channel for video {video_id!r}: not ingested and no --channel given")
    conn.execute(
        "INSERT INTO wl_mismatch (video_id, channel, title, my_tier, decision, note, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(video_id) DO UPDATE SET "
        "channel=excluded.channel, title=excluded.title, my_tier=excluded.my_tier, "
        "decision=excluded.decision, note=excluded.note, recorded_at=excluded.recorded_at",
        [video_id, channel, title, my_tier, decision, note or None, _now()],
    )
    conn.commit()
    return {"video_id": video_id, "channel": channel, "title": title,
            "my_tier": my_tier, "decision": decision}


def trusted_channels(conn, threshold: int = TRUST_THRESHOLD) -> set[str]:
    """Channels Jelle has contradicted on >= threshold DISTINCT videos.

    COUNT is over the PK (video_id), so re-recording the same video can never push a channel
    over the line — trust requires distinct contradicted videos."""
    ensure_wl_mismatch(conn)
    rows = conn.execute(
        "SELECT channel, COUNT(*) AS n FROM wl_mismatch "
        "GROUP BY channel HAVING n >= ?", [threshold]
    ).fetchall()
    return {r["channel"] for r in rows}


def _print_list(conn) -> None:
    ensure_wl_mismatch(conn)
    rows = conn.execute(
        "SELECT channel, video_id, title, my_tier, decision, recorded_at "
        "FROM wl_mismatch ORDER BY channel, recorded_at").fetchall()
    trusted = trusted_channels(conn)
    print(f"# wl_mismatch — {len(rows)} contradiction(s), "
          f"{len(trusted)} trusted channel(s) (threshold {TRUST_THRESHOLD})")
    for r in rows:
        star = " *TRUSTED*" if r["channel"] in trusted else ""
        print(f"  [{r['my_tier'] or '?'}] {r['channel']}{star} | {r['title'] or r['video_id']} "
              f"| {r['decision']} | {r['recorded_at'][:10]}")
    if trusted:
        print("trusted_channels: " + ", ".join(sorted(trusted)))


@call_parse
def main(
    video: str = "",        # video id or YouTube URL of the contradicted video
    decision: str = "kept", # Jelle's decision: kept | watched
    note: str = "",         # optional free-text why he kept it
    channel: str = "",      # explicit channel (only needed if the video was never ingested)
    list: bool = False,     # instead of recording: print current mismatches + trusted channels
    db: str = "",           # consume.db path override (tests use a temp db)
):
    "Record a Watch-Later prune-advice contradiction (or --list the ledger). 3 distinct contradictions on a channel trust it (suppressed from future prune notes)."
    conn = connect(db or None)
    init_schema(conn)
    ensure_wl_mismatch(conn)
    if list:
        _print_list(conn)
        conn.close()
        return
    if not video:
        print("error: --video <id-or-url> is required (or use --list)", file=sys.stderr)
        conn.close()
        sys.exit(2)
    vid = extract_video_id(video)
    try:
        res = record_mismatch(conn, vid, decision, note, channel=channel or None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        conn.close()
        sys.exit(3)
    trusted = trusted_channels(conn)
    conn.close()
    now_trusted = res["channel"] in trusted
    print(f"recorded video={res['video_id']} channel={res['channel']!r} "
          f"tier={res['my_tier'] or '?'} decision={res['decision']} "
          f"trusted={'yes' if now_trusted else 'no'}")
    if now_trusted:
        print(f"channel {res['channel']!r} is now TRUSTED — its videos are suppressed "
              f"from future prune notes.")
