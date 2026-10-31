---
status: accepted
date: 2026-10-31
---
# The passage is the unit that is embedded; the chapter is the unit that is returned

The first idea was one vector per chapter. A chapter is often 5k to 15k tokens, `bge-m3` truncates at 8k, and the Merlinshat questions ("which passage supports this lesson") are finer than a chapter. So the worker cuts each chapter into passages of about 500 tokens with 50 tokens overlap at paragraph boundaries, embeds those, and search groups hits by chapter with the best passage as the chapter's score. Chapters come from the document's own structure: EPUB table of contents (spine files as fallback), PDF outline (ten-page groups as fallback), level-one headings for Braincave notes and Library texts (one chapter as fallback).
