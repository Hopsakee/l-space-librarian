---
status: accepted
date: 2026-10-31
---
# Vectors live in l-space.db as float16 blobs searched with numpy

Book, Library and note vectors go into `l-space.db` next to the items they may link to, not into a second database file, because two files mean two things for the backup job, the worker and every link to keep in step (Jelle, 2026-10-31). Stored as little-endian float16 blobs, 2 KB per `bge-m3` passage, about 400 MB for the expected 180k passages, and searched by a numpy dot product, which stays under a second at that size. `sqlite-vec` is not in the approved wheel store; it becomes worth approving only if search exceeds about a second. `items.embedding` keeps its float32 format for one vector per item.
