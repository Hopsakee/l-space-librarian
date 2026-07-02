# l-space-librarian

The **Librarian** that collects consumable items into **L-space** — a local
SQLite index (`l-space.db`) for prioritising what to read/watch without spending
hours triaging. ("L-space" is the library-space of Unseen University in Terry
Pratchett's Discworld.)

It ingests two firehoses into one index:

- **Readwise Reader** articles (via a sibling `readwise-tools` package)
- **YouTube Watch Later** (via `yt-dlp` + browser cookies) — read by relevance to
  your current focus, content-rated from the transcript, so the good ones surface
  and the low-value ones can be pruned.

The index is a **read-model**: it never writes into recall.it, Obsidian, or any
external service. It only reads sources and writes the local `l-space.db`.

## Install

```bash
cd ~/Code/l-space-librarian
uv sync
```

Readwise access is reused from a sibling `readwise-tools` package (a uv path
dependency); its API token is resolved at runtime and never stored in this repo.

## Commands

```bash
cs-init                              # create the l-space.db schema (idempotent)
cs-ingest --location new             # ingest a Reader location's docs into `items`
cs-groundtruth --location later      # pull every _rating/* tag into `ratings`
cs-fts --rebuild                     # (re)build the FTS5 index for BM25 search
cs-watchlater --browser <b> --limit N  # triage YouTube Watch Later into `items`
```

`l-space.db` defaults to `~/code_data/l-space-librarian/l-space.db`; override with
`--db PATH` or the `CONSUME_DB` env var.

## Schema

- **items** — one row per consumable item (`source` = `readwise` | `youtube-wl`).
  Ingest-owned columns (title, summary, location, …) refresh on re-ingest;
  enrichment columns (`quality_auto`, `quality_self`, `topic_tags`, `embedding`,
  `consumed`) are never clobbered by re-ingest.
- **ratings** — every `_rating/<tier>[/<rater>]` tag, one row per `(item_id, rater)`.
  `rater` is the model name (e.g. `claude-haiku-yt`) or `bare` for an un-attributed
  tag. The ground-truth substrate for evaluating the quality prompt.
- **queries** / **query_labels** — focused reading-questions and per-query relevance
  feedback, harvested as a byproduct of use (the retrieval bake-off ground truth).

## Notes

- The DB is **not** committed (see `.gitignore`); it lives outside the repo and is
  backed up separately.
- No secrets, tokens, cookies, or personal data live in this repository.
