"""cs-watchlater — YouTube Watch Later → consume triage.

SLICE 1 (complete): read WL via yt-dlp+cookies, filter by RELEVANCE
(title+channel+description vs Jelle's current focus, Haiku, high-recall), and
ingest the relevant videos into consume.db `items` as `youtube-wl` candidates.

SLICE 2 (complete, see ISA 20260701-watchlater-consume): for each relevant item,
fetch the transcript and run `estimate-quality` on the CONTENT (transcript only,
never metadata); the tier lands in `ratings` (rater 'claude-haiku-yt') so
_READING_ADVICE surfaces S/A picks in its NEW lane, while B-or-lower relevant
videos go into a prune note ("safe to delete from Watch Later") the nightly
wrapper publishes to Braincave. Non-relevant videos never reach the transcript
step, and nothing is ever deleted from YouTube.

Design rules (ISA Out of Scope / Constraints):
  * RELEVANCE may use metadata; QUALITY may NOT — quality is about content, so it
    is computed from the transcript only, and only in slice 2. This module never
    rates quality.
  * Read-only against YouTube. Never deletes/edits/POSTs. Prune is a suggestion
    Jelle acts on manually.
  * Non-relevant videos are simply not ingested (no writes, no transcript, no
    prune entry). Relevance is temporal; only quality earns a delete suggestion.
  * Reuses consume_selection.db (connect/init_schema) and readwise_tools.infer
    (the single sanctioned Inference.ts path). No bespoke DB or LLM code.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema
from consume_selection.ingest import _INGEST_COLS
from readwise_tools.infer import extract_json, run_inference
from readwise_tools.prompt_sync import load_prompt
from readwise_tools.rate_document import rate_text

# --- constants --------------------------------------------------------------

WL_URL = "https://www.youtube.com/playlist?list=WL"
PAI_USER = Path.home() / ".claude" / "PAI" / "USER"
EXEC_LOG = Path("~/.claude/PAI/MEMORY/SKILLS/execution.jsonl").expanduser()

# Prune-note staging. cs-watchlater writes the note here (a PAI-internal path,
# NOT the vault); the nightly wrapper publishes it into Braincave 0_Inbox/Hoggle/
# via PublishToBraincave.ts — the ONLY sanctioned write path to the vault.
PRUNE_DIR = Path("~/.claude/PAI/MEMORY/STATE/watchlater").expanduser()

# Quality tier lands in `ratings` under this rater so _READING_ADVICE reads it
# from the same lane as Readwise auto-ratings (`_rating/<tier>/<model>`).
RATER = "claude-haiku-yt"
QUALITY_PROMPT = "estimate-quality"

# Preferred transcript languages, priority order — mirrors _TOLIBRARY_YOUTUBE.
_PREFERRED_LANGS = ["en", "nl", "de", "fr", "es", "pt", "it", "ja", "zh"]

# `_INGEST_COLS` (the ingest-owned column list) is imported from consume_selection.ingest
# so the two upserts can never drift; enrichment cols (quality_auto, embedding, …)
# are never in it, so re-ingest never clobbers a rating/embedding.

_REL_SYSTEM = (
    "You decide whether a YouTube video is POSSIBLY RELEVANT to the user's "
    "current interests, goals, and active projects. Be HIGH-RECALL: if it "
    "plausibly connects to any of them, answer relevant=true. Only reject a "
    "clear non-fit (pure entertainment, unrelated hobby, off-topic). Judge from "
    "the title, channel, and description only — this is a relevance gate, NOT a "
    "quality judgment. Respond with ONE JSON object: "
    '{"relevant": true|false, "reason": "<max 10 words>"}.'
)


# --- helpers ----------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def build_ytdlp_cmd(playlist_url: str, browser: str, limit: int) -> list[str]:
    """Assemble the read-only yt-dlp command. Isolated so it is unit-testable
    without invoking yt-dlp (ISC-2). Contains NO mutating flag (ISC-21)."""
    # --ignore-no-formats-error: we want METADATA ONLY (title/channel/description),
    # never a download. Without it, --dump-json resolves a playable format per video
    # and aborts with "Requested format is not available" on videos YouTube won't
    # serve a format for on this yt-dlp build — silently skipping every one (read=0).
    cmd = ["yt-dlp", "--dump-json", "--skip-download",
           "--ignore-no-formats-error", "--ignore-errors", "--no-warnings"]
    if limit:
        cmd += ["-I", f"1:{limit}"]
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(playlist_url)
    return cmd


def _parse_ndjson(stdout: str) -> list[dict]:
    """Parse yt-dlp NDJSON (one video object per line) into item dicts."""
    items: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = d.get("id")
        if not vid:
            continue
        items.append({
            "video_id": vid,
            "title": d.get("title") or "",
            "channel": d.get("channel") or d.get("uploader") or "",
            "description": d.get("description") or "",
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return items


def read_playlist(playlist_url: str, browser: str, limit: int,
                  timeout: int = 300) -> tuple[list[dict], int]:
    """Run yt-dlp and return (items, returncode). WL needs cookies; a public
    playlist (no browser) exercises the identical parse path (ISC-5)."""
    cmd = build_ytdlp_cmd(playlist_url, browser, limit)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return _parse_ndjson(proc.stdout), proc.returncode


def build_relevance_context(limit_chars: int = 6000) -> str:
    """Assemble Jelle's current focus/goals/projects into a prompt string."""
    parts: list[tuple[str, Path]] = [
        ("Huidige focus & interesses", PAI_USER / "Focus.md"),
        ("Doelen (Telos)", PAI_USER / "Telos" / "PrincipalTelos.md"),
        ("Actieve projecten", PAI_USER / "Projects" / "README.md"),
    ]
    chunks = []
    for label, path in parts:
        if path.exists():
            chunks.append(f"## {label}\n{path.read_text(encoding='utf-8')[:limit_chars]}")
    return "\n\n".join(chunks)


def judge_relevance(item: dict, context: str) -> tuple[bool, str]:
    """High-recall relevance verdict via Inference.ts (Haiku). On a parse/LLM
    failure, default to RELEVANT (high-recall bias) rather than dropping."""
    user = (
        f"CONTEXT (the user's current interests, goals, active projects):\n{context}\n\n"
        f"---\nVIDEO:\nTitle: {item['title']}\nChannel: {item['channel']}\n"
        f"Description: {item['description'][:1500]}\n\n"
        "Is this video possibly relevant to the user's current focus? JSON only."
    )
    raw = run_inference(_REL_SYSTEM, user, level="fast", inference_timeout_ms=60000)
    try:
        v = extract_json(raw)
        return bool(v.get("relevant")), str(v.get("reason", ""))[:80]
    except Exception:
        return True, "parse-fallback (kept, high-recall)"


def ingest_item(conn, item: dict) -> None:
    """Idempotent upsert of ONE relevant video into consume.db `items`.
    Enrichment columns (quality_auto, ratings, embedding, …) are never touched."""
    row = {
        "id": item["video_id"],
        "source": "youtube-wl",
        "title": item["title"],
        "author": item["channel"],
        "url": item["url"],
        "source_url": item["url"],
        "site_name": "youtube",
        "summary": item["description"][:2000],
        "item_type": "video",
        "word_count": None,
        "location": "watchlater",
        "added_at": None,
        "ingested_at": _now(),
    }
    cols = ["id"] + _INGEST_COLS
    placeholders = ", ".join(["?"] * len(cols))
    update_set = ", ".join(f"{c}=excluded.{c}" for c in _INGEST_COLS)
    conn.execute(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set}",
        [row[c] for c in cols],
    )


def fetch_transcript(video_id: str) -> str | None:
    """Return the transcript text for a video, or None if unavailable.

    Mirrors the `_TOLIBRARY_YOUTUBE` 1.x instance API: try the preferred-language
    list, fall back to the first available track, and on any failure (disabled,
    none, unavailable, IP-block) return None so the caller degrades gracefully
    (logged `no_transcript`, not pruned, resumable next run). Broad except is
    deliberate — youtube-transcript-api raises many specific subclasses and a
    transcript miss must never crash an unattended batch.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # noqa: PLC0415
    except ImportError:
        return None
    api = YouTubeTranscriptApi()
    try:
        result = api.fetch(video_id, languages=_PREFERRED_LANGS)
    except Exception:
        try:
            first = next(iter(api.list(video_id)))
            result = first.fetch()
        except Exception:
            return None
    snippets = [s.text.strip() for s in result.snippets if s.text and s.text.strip()]
    if not snippets:
        return None
    return " ".join(snippets)


def write_rating(conn, item_id: str, tier: str) -> None:
    """Idempotent upsert of ONE auto-rating into `ratings`, keyed on
    (item_id, rater). Tier is lowercased (the `ratings` convention _READING_ADVICE
    reads); raw_tag mirrors the Readwise `_rating/<tier>/<model>` shape."""
    t = tier.lower()
    conn.execute(
        "INSERT INTO ratings (item_id, tier, rater, raw_tag) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(item_id, rater) DO UPDATE SET "
        "tier=excluded.tier, raw_tag=excluded.raw_tag",
        [item_id, t, RATER, f"_rating/{t}/{RATER}"],
    )


def _quality_reason(quality: dict, fallback: str = "low quality") -> str:
    """Pull a one-line human reason out of the estimate-quality verdict. The
    rubric emits 'Why this tier' + 'Verdict'; match case-insensitively over a
    preferred order before falling back to the relevance reason."""
    if isinstance(quality, dict):
        lower = {k.lower(): v for k, v in quality.items()}
        for key in ("why this tier", "verdict", "summary", "reason",
                    "justification"):
            val = lower.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:160]
    return fallback


def _md_cell(text: str) -> str:
    """Make a string safe for a one-line markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def write_prune_note(entries: list[dict], prune_dir: Path, date: str) -> Path:
    """Write the 'safe to delete from Watch Later' note (relevant but B-or-lower).

    This is a SUGGESTION artifact — it never deletes anything. Written to a
    PAI-internal staging path; the nightly wrapper publishes it into Braincave
    0_Inbox/Hoggle/ via PublishToBraincave.ts (the sanctioned vault write path)."""
    prune_dir.mkdir(parents=True, exist_ok=True)
    path = prune_dir / f"watch-later-prune-{date}.md"
    lines = [
        f"# Watch Later — safe to delete ({date})",
        "",
        "> These videos are **relevant** to your current focus but scored **tier B "
        "or lower** on content quality after reading the transcript. You can safely "
        "remove them from your YouTube Watch Later.",
        ">",
        "> **This list never deletes anything — you delete them yourself in YouTube.**",
        "",
        "| Tier | Title | Channel | URL | Why |",
        "|------|-------|---------|-----|-----|",
    ]
    for e in entries:
        lines.append(
            f"| {e['tier']} | {_md_cell(e['title'])} | {_md_cell(e['channel'])} "
            f"| {e['url']} | {_md_cell(e['reason'])} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _log_run(counts: dict) -> None:
    try:
        EXEC_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"timestamp": _now(), "v": 1, "tool": "cs-watchlater",
               "event_type": "run", **counts}
        with EXEC_LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# --- CLI --------------------------------------------------------------------

@call_parse
def main(
    browser: str = "",              # browser for --cookies-from-browser (WL needs it)
    playlist_url: str = WL_URL,     # override for public-playlist testing
    limit: int = 0,                 # cap videos processed (0 = all)
    dry_run: bool = False,          # read + judge relevance only, write nothing / no cost
    db: str = "",                   # consume.db path override (tests use a temp db)
    prune_dir: str = "",            # where the prune note is staged (default PRUNE_DIR)
):
    "Read YouTube Watch Later, keep the relevant videos, rate their content, and split them into recommend + prune lanes."
    conn = connect(db or None)
    init_schema(conn)
    items, rc = read_playlist(playlist_url, browser, limit)
    context = build_relevance_context()
    pdir = Path(prune_dir).expanduser() if prune_dir else PRUNE_DIR
    # Load the estimate-quality prompt ONCE (default rate_text pulls the repo +
    # re-reads the file per call — a git round-trip per video). pull=False: an
    # unattended nightly batch should not git-pull mid-run. None on dry-run (no rating).
    # A MISSING prompt repo must NOT crash the whole run to a silent "0/0/0" (that
    # is the silent-regression failure mode): catch it, leave quality_prompt None,
    # and every relevant item then counts as rate_failed so the wrapper alarms.
    quality_prompt = None
    if not dry_run:
        try:
            quality_prompt = load_prompt(QUALITY_PROMPT, pull=False)
        except Exception as exc:
            print(f"prompt_error=1 ({type(exc).__name__}: {str(exc)[:120]})",
                  file=sys.stderr)

    read = len(items)
    relevant = ingested = rated = sa = no_transcript = rate_failed = failed = 0
    prune_entries: list[dict] = []

    for it in items:
        try:
            rel, reason = judge_relevance(it, context)
        except Exception:
            failed += 1
            continue
        if not rel:
            continue          # ISC-34: non-relevant → no transcript, no rating, no prune
        relevant += 1
        if dry_run:
            continue          # dry-run stays relevance-only: no transcript, no LLM cost, no writes

        ingest_item(conn, it)
        ingested += 1

        # --- quality: content-only (transcript), never metadata (ISC-35) -----
        if quality_prompt is None:   # prompt repo failed to load — can't rate; resumable
            rate_failed += 1
            continue
        transcript = fetch_transcript(it["video_id"])
        if not transcript:
            no_transcript += 1        # ISC-24: graceful, not pruned, resumable next run
            continue
        res = rate_text(transcript, prompt_body=quality_prompt,
                        level="fast", model_slug=RATER)
        tier = res.get("tier")        # rate_text already validates ∈ {S,A,B,C,D} (ISC-26)
        if not tier:
            rate_failed += 1
            continue
        write_rating(conn, it["video_id"], tier)   # ISC-27
        rated += 1
        if tier in ("S", "A"):
            sa += 1                    # recommendable via _READING_ADVICE (already ingested + rated)
        else:                          # B/C/D relevant → prune lane (ISC-29)
            prune_entries.append({
                "tier": tier, "title": it["title"], "channel": it["channel"],
                "url": it["url"], "reason": _quality_reason(res.get("quality", {}), reason),
            })

    if not dry_run:
        conn.commit()
    conn.close()

    prune_path = None
    if prune_entries and not dry_run:
        prune_path = write_prune_note(prune_entries, pdir, today_iso())

    pruned = len(prune_entries)
    counts = {"read": read, "relevant": relevant, "ingested": ingested,
              "rated": rated, "sa": sa, "pruned": pruned,
              "no_transcript": no_transcript, "rate_failed": rate_failed,
              "failed": failed, "dry_run": dry_run, "ytdlp_rc": rc,
              "prune_note": str(prune_path) if prune_path else ""}
    print(f"read={read} relevant={relevant} ingested={ingested} rated={rated} "
          f"sa={sa} pruned={pruned} no_transcript={no_transcript} "
          f"rate_failed={rate_failed} failed={failed}")
    if prune_path:
        print(f"prune_note={prune_path}")
    _log_run(counts)
