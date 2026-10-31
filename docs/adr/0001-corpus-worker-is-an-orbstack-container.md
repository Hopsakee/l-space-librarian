---
status: accepted
date: 2026-10-31
---
# The embedding worker runs as an OrbStack container, and the box only embeds queries through the broker

Embedding runs on the Mac Mini with Ollama (`bge-m3`), not in the box: the box has no route to the host and no GPU-free budget for GBs of books. The nightly worker that reads Calibre, the Library and Braincave and writes `l-space.db` is an OrbStack container on `hoggle-net`, like `consume-web`, never a macOS `launchd` process: SQLite's WAL shared memory does not cross the VM boundary, so a host writer next to box readers corrupts the database. At query time the box asks the broker for one thing only, `POST /ollama/embed` with a fixed body, gets a vector back and searches `l-space.db` itself. The broker stays a route table; a firewall hole to Ollama's port was rejected because it would also expose pull, push and create.

**Considered:** host `launchd` worker (WAL hazard), in-box `onnxruntime` embeddings (needs new wheels and model files, and a CPU the box shares with everything else), a `~/transport` file handoff (Jelle: transport is for controlled file exchange, not a data bus).
