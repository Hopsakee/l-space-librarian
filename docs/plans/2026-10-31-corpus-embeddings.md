# Corpus embeddings: plan

Decided with Jelle 2026-10-31 (grill session). Vocabulary in `GLOSSARY.md`; the three load-bearing choices in `docs/adr/0001`–`0003`.

## Done means

1. `corpus search "<question>"` returns ranked chapters with book or note, chapter title, top-3 passages and character offsets, by hybrid search (BM25 + `bge-m3` vectors, reciprocal rank fusion).
2. `_LEARNING_GUIDE` uses it to answer "which of my books and notes cover X"; `_TEACH` uses it to pull candidate passages whose citations `VerifyCitations.py` can check.
3. `_CONSUME_ADVICE candidates --question` ranks by hybrid search instead of BM25 alone.
4. An eval set of 20 queries Jelle writes (`tests/eval/queries.yaml`: query, expected documents) shows hybrid against BM25-only, precision@10.

## Facts the plan rests on

- Mac Mini: 2018 Intel i7-8700B, 32 GB, CPU only. Ollama from the host installer. Calibre at `~/Drive-bup/Calibre`, about 600 books. Library: 309 texts, 6.2M words. Braincave: 9,047 notes.
- Box: wheels-only Python (`numpy`, `onnxruntime`, `pymupdf` approved), no route to the host except the broker and `~/transport`. `l-space.db` is 32 MB and backed up nightly whole.
- Existing: `cs-embed` (Ollama backend, `items.embedding`), `_CONSUME_ADVICE --fit-backend embedding`, FTS5 on `items`.

## Architecture

```
host: Ollama (bge-m3)  <── corpus-worker container (nightly 01:00–06:00)
                         reads  ~/Drive-bup/Calibre (ro), ~/Drive/Library (ro), ~/Obsidian/Braincave (ro)
                         writes ~/data/l-space-librarian/l-space.db  (documents, chapters, passages, passages_fts)
       Ollama  <── broker route POST /ollama/embed  <── box: corpus search / skills (numpy over passages)
```

Schema, all in `l-space.db`:

- `documents(id, source {calibre|library|braincave}, source_ref, item_id NULL→items, title, authors, language, content_hash, embedded_at, embedding_model)`
- `chapters(id, document_id, ordinal, title, char_start, char_end)`
- `passages(id, chapter_id, ordinal, char_start, char_end, text, embedding BLOB float16)`
- `passages_fts` (FTS5 over `passages.text`, external content)

Linking a Calibre book to an item: ISBN when both have one, else normalised title + first author, else unlinked; the unlinked list is printed once for Jelle.

## Phases, each shipped and used before the next

| # | Scope | Ships |
|---|---|---|
| 0 | Host handoff (Jelle, on the Mac): Ollama running with `bge-m3` pulled; broker route `POST /ollama/embed` (body keys `model`, `input` only, model pinned server-side); box mount `~/Drive-bup/Calibre` → `~/Calibre` ro; `corpus-worker` service in `consume_web/compose.yaml` with the three ro mounts and `~/data` | a handoff file in `~/transport/` |
| 1 | `corpus/` package: extract (EPUB via TOC/spine, PDF via outline/10-page groups, markdown via H1), chunk, embed, store; `corpus ingest --source calibre|library --limit N`; `corpus search`; eval harness. First evening: 20 books, throughput printed. Backfill resumes nightly by content hash | PR 1 |
| 2 | Braincave: one document per note, H1 chapters; `_LEARNING_GUIDE` and `_TEACH` wrappers call `corpus search` | PR 2 + skills commit |
| 3 | `cs-embed` through the broker route for `items` (title + summary); `_CONSUME_ADVICE candidates --question` fuses BM25 and vectors; eval report | PR 3 + skills commit |
| 4 | Later, separate decision: persist podcast/YouTube/Readwise full text, then embed it | not in this plan |

## Revisit triggers

- Throughput measurement says the Calibre backfill needs more than ~10 nights → bake off `embeddinggemma` against `bge-m3` on the eval set; the model is per-vector, switching is a rebuild.
- numpy search over passages exceeds ~1 s → approve `sqlite-vec`.
- A scanned PDF without a text layer is logged and skipped; OCR is out of scope.

## Not doing

Web UI; a local reranker; Zotero exports, Hopswiki, ToRead as corpora; embedding inside the box; a second database file.
