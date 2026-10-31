# l-space-librarian

The catalogue of what Jelle could consume (the firehose, the consume list) and, since 2026-10-31, the searchable text of what he owns: books, Library texts and notes. One SQLite database, `l-space.db`.

## Language

### Consume selection

**Item**:
One thing in the firehose that could be consumed: an article, video, podcast episode, book, note. The row in `items`.
_Avoid_: document, entry, candidate (a candidate is an item that passed the pickable rule)

**Consume item**:
An item Jelle has committed to, tracked to done on the consume list.

### Corpus

**Document**:
One source text that was split into passages: a book from Calibre, a Library text, a vault note. A document may be linked to an item; it need not be.
_Avoid_: file, source, book (a book is one kind of document)

**Chapter**:
The author's own division of a document, as its table of contents gives it. The unit shown in search results. A document without a table of contents has one chapter.
_Avoid_: section, part

**Passage**:
A stretch of about 500 tokens inside one chapter, cut at paragraph boundaries, overlapping its neighbours slightly. The unit that is embedded and matched.
_Avoid_: chunk, segment, snippet

**Hybrid search**:
One query answered by BM25 over passages and by vector similarity over the same passages, fused into one ranking, grouped by chapter.
_Avoid_: semantic search, RAG
