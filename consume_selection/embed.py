"""cs-embed / cs-match — semantic embeddings over consume.db items.

Provider abstraction so the backend is swappable with one env var:

  CONSUME_EMBED_BACKEND = sentence-transformers (default) | ollama
  CONSUME_EMBED_MODEL   = model name (default intfloat/multilingual-e5-small)
  OLLAMA_URL            = http://localhost:11434  (only for the ollama backend)

Today: `sentence-transformers` runs in-process — multilingual (bridges Dutch
interests ↔ English content), free, offline. Default model is the SMALL e5
(~470MB) so it embeds fine on the NUC; set CONSUME_EMBED_MODEL=BAAI/bge-m3 on
beefier hardware for higher quality.

Later (MacMini + local Ollama): set CONSUME_EMBED_BACKEND=ollama and
CONSUME_EMBED_MODEL=bge-m3 (or nomic-embed-text). The OllamaEmbedder below is
ready. Each item row records `embedding_model`; switching models = one
`cs-embed --rebuild` (a different model is a different vector space — inherent to
embeddings, not a code change). Nothing else downstream changes.
"""
from __future__ import annotations

import os
import struct

from fastcore.script import call_parse

from consume_selection.db import connect, init_schema

DEFAULT_BACKEND = os.environ.get("CONSUME_EMBED_BACKEND", "sentence-transformers")
DEFAULT_MODEL = os.environ.get("CONSUME_EMBED_MODEL", "intfloat/multilingual-e5-small")


# --- vector (de)serialisation: little-endian float32 blob -------------------

def pack_vec(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *[float(x) for x in vec])


def unpack_vec(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob)//4}f", blob))


# --- provider abstraction ----------------------------------------------------

class Embedder:
    """Interface: .model_name, .embed(texts, kind) -> list[list[float]]."""
    model_name = "abstract"

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        raise NotImplementedError


class SentenceTransformersEmbedder(Embedder):
    """In-process local embeddings. Lazy-imports so the core CLIs stay light."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise SystemExit(
                "sentence-transformers not installed. Run: uv sync --extra embed"
            )
        self._m = SentenceTransformer(model_name)
        self._is_e5 = "e5" in model_name.lower()  # e5 wants query:/passage: prefixes

    def embed(self, texts, kind="passage"):
        if self._is_e5:
            prefix = "query: " if kind == "query" else "passage: "
            texts = [prefix + (t or "") for t in texts]
        vecs = self._m.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]


class OllamaEmbedder(Embedder):
    """HTTP embeddings via a local Ollama server (the MacMini path). Ready to use."""

    def __init__(self, model_name: str = "bge-m3", url: str | None = None):
        self.model_name = model_name
        self.url = (url or os.environ.get("OLLAMA_URL", "http://localhost:11434")).rstrip("/")

    def embed(self, texts, kind="passage"):
        import urllib.request
        import json as _json
        out = []
        for t in texts:
            req = urllib.request.Request(
                f"{self.url}/api/embeddings",
                data=_json.dumps({"model": self.model_name, "prompt": t or ""}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                out.append(_json.loads(r.read())["embedding"])
        return out


_OLLAMA_DEFAULT_MODEL = "bge-m3"


def get_embedder(backend: str = DEFAULT_BACKEND, model: str = DEFAULT_MODEL) -> Embedder:
    if backend == "ollama":
        # Only substitute the ollama default when the caller left model at the
        # sentence-transformers compile-time default. Comparing against
        # DEFAULT_MODEL broke when CONSUME_EMBED_MODEL was set (it redefines
        # DEFAULT_MODEL, so an explicit env model equalled it and was ignored).
        name = model if model != "intfloat/multilingual-e5-small" else _OLLAMA_DEFAULT_MODEL
        return OllamaEmbedder(model_name=name)
    return SentenceTransformersEmbedder(model_name=model)


# --- storage migration -------------------------------------------------------

def ensure_embedding_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    if "embedding_model" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN embedding_model TEXT;")
        conn.commit()


# --- operations --------------------------------------------------------------

def embed_items(conn, embedder: Embedder, rebuild: bool = False, limit: int = 0) -> int:
    """Embed item summaries. Skips items already embedded with the SAME model
    unless rebuild=True. Returns number of items (re)embedded."""
    ensure_embedding_columns(conn)
    where = "summary IS NOT NULL AND summary <> ''"
    if not rebuild:
        where += " AND (embedding IS NULL OR embedding_model IS NOT ? OR embedding_model <> ?)"
        params = (embedder.model_name, embedder.model_name)
    else:
        params = ()
    sql = f"SELECT id, summary FROM items WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    done = 0
    BATCH = 64
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        vecs = embedder.embed([r["summary"] for r in batch], kind="passage")
        for r, v in zip(batch, vecs):
            conn.execute(
                "UPDATE items SET embedding=?, embedding_model=? WHERE id=?",
                (pack_vec(v), embedder.model_name, r["id"]),
            )
        conn.commit()
        done += len(batch)
    return done


def match(conn, embedder: Embedder, query: str, k: int = 10) -> list[dict]:
    """Embed the query, cosine-rank items embedded with the same model."""
    ensure_embedding_columns(conn)  # cs-match before any cs-embed run else 'no such column: embedding_model'
    import numpy as np
    qv = np.array(embedder.embed([query], kind="query")[0], dtype="float32")
    qn = qv / (np.linalg.norm(qv) + 1e-9)
    rows = conn.execute(
        "SELECT id, title, embedding FROM items WHERE embedding IS NOT NULL AND embedding_model=?",
        (embedder.model_name,),
    ).fetchall()
    scored = []
    for r in rows:
        v = np.array(unpack_vec(r["embedding"]), dtype="float32")
        v = v / (np.linalg.norm(v) + 1e-9)
        scored.append((float(qn @ v), r["id"], r["title"]))
    scored.sort(reverse=True)
    return [{"score": s, "id": i, "title": t} for s, i, t in scored[:k]]


@call_parse
def main(
    query: str = "",        # cosine-match query; omit with --rebuild
    rebuild: bool = False,   # (re)embed ALL items with the current model
    limit: int = 0,
    backend: str = DEFAULT_BACKEND,
    model: str = DEFAULT_MODEL,
    db: str = "",
):
    "Embed item summaries (--rebuild) or cosine-match a query against them."
    conn = connect(db or None)
    init_schema(conn)
    embedder = get_embedder(backend, model)
    if not query:
        n = embed_items(conn, embedder, rebuild=rebuild, limit=limit)
        conn.close()
        print(f"embedded {n} items with {embedder.model_name} (backend={backend})")
        return
    hits = match(conn, embedder, query, limit or 10)
    conn.close()
    print(f"{len(hits)} hit(s) for {query!r} ({embedder.model_name}):")
    for i, h in enumerate(hits, 1):
        print(f"  {i}. [{h['score']:.3f}] {h['id']}  {(h['title'] or '')[:60]}")
