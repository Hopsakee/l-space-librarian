# consume-selection

A local **consume-index**: prioritise what to read/watch/listen from the Readwise
firehose without spending hours triaging. This repo is **stage 1** of the system
specced in `~/.claude/PAI/MEMORY/WORK/20260606-consume-selection/ISA.md` — the
index substrate, ingestion, and ground-truth puller. Embeddings, scoring (the
crown-jewel quality + topics prompts on `Inference.ts`), the retrieval bake-off,
the `_INTERVIEW_CONSUME` skill, and the shortlist generator come in later slices.

The index is a **read-model**. It never writes content into recall.it or Hopswiki.

## Install

```bash
cd ~/Code/consume-selection
uv sync
```

Readwise access is reused from `../readwise-tools` (a uv path dependency). The
Readwise token is resolved by that package at runtime and never printed.

## Commands

```bash
cs-init                              # create the consume.db schema (idempotent)
cs-ingest --location new             # ingest a Reader location's docs into `items`
cs-ingest --from-json dump.json      # ingest a cached rw-list dump instead
cs-groundtruth --location later      # pull every _rating/* tag into `ratings`
```

`consume.db` defaults to `~/data/sqlite/consume.db` (matches the local data-db
convention); override with `--db PATH` or the `CONSUME_DB` env var.

## Schema

- **items** — one row per consumable item. Ingest-owned columns (title, summary,
  location, …) refresh on re-ingest; enrichment columns (`quality_auto`,
  `quality_self`, `topic_tags`, `embedding`, `consumed`) are written by later
  slices and are never clobbered by re-ingest.
- **ratings** — every `_rating/<tier>[/<rater>]` tag, one row per `(item_id, rater)`.
  `rater` is the model name (e.g. `mistrall-small-4`) or `bare` for an
  un-attributed tag. This is the ground-truth substrate for evaluating the
  quality prompt.

> **Open question for Jelle:** the real Readwise data has no `/zelf` tag and no
> dual-tagging — bare `_rating/<tier>` (n8n era, Dec 2025–Feb 2026) and
> `_rating/<tier>/<model>` (Feb–Jun 2026) never co-occur. Which rater string
> counts as *your own* ground-truth rating is recorded but not assumed.
