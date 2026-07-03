"""Semantic embeddings via NVIDIA-hosted BGE-M3 (1024-dim) — for semantic dedup of news/signals (collapse
echoed copies of the same event into ONE, better than lexical Jaccard) and a foundation for RAG over the
brain. Aggressively cached (each unique text embeds once, persisted) + batched + bounded, and degrades to
None if no key so callers fall back to lexical methods. Uses NV_BGE_KEY (falls back to NVIDIA_API_KEY).
"""
from __future__ import annotations

import hashlib
import math
import os

try:
    import requests
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

URL = "https://integrate.api.nvidia.com/v1/embeddings"
MODEL = "baai/bge-m3"
_CACHE_PATH = "state/embed_cache.json"
_cache = None


def _key() -> str:
    return os.getenv("NV_BGE_KEY") or os.getenv("NVIDIA_API_KEY") or ""


def available() -> bool:
    return _HAVE and bool(_key())


def _load() -> dict:
    global _cache
    if _cache is None:
        from .persist import load_json
        _cache = load_json(_CACHE_PATH, {})
    return _cache


def _save():
    from .persist import save_json
    if _cache is not None:
        save_json(_CACHE_PATH, _cache)


def _h(t: str) -> str:
    return hashlib.sha1(t.encode("utf-8", "ignore")).hexdigest()[:16]


def embed(texts, input_type: str = "query", max_batch: int = 32, max_texts: int = 64):
    """Return a list of vectors (or None per text on failure), cached by text hash. Bounded to
    `max_texts` per call so it can never blow the free quota."""
    if not available() or not texts:
        return [None] * len(texts)
    texts = list(texts)[:max_texts]
    cache = _load()
    out = [None] * len(texts)
    todo, idx = [], []
    for i, t in enumerate(texts):
        h = _h(t)
        if h in cache:
            out[i] = cache[h]
        else:
            todo.append(t)
            idx.append(i)
    for s in range(0, len(todo), max_batch):
        batch = todo[s:s + max_batch]
        try:
            r = requests.post(URL, timeout=20,
                              headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
                              json={"input": batch, "model": MODEL, "input_type": input_type,
                                    "encoding_format": "float", "truncate": "END"})
            if r.status_code != 200:
                continue
            for j, d in enumerate(r.json().get("data", [])):
                v = d.get("embedding")
                if v:
                    gi = idx[s + j]
                    out[gi] = v
                    cache[_h(todo[s + j])] = v
        except Exception:
            continue
    _save()
    return out


def cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dedupe(texts, threshold: float = 0.82) -> list:
    """Return the indices of texts that are semantically NOVEL (near-duplicates collapsed). Texts that
    failed to embed are kept (fail-open). Used to fuse echoed copies of the same news event into one."""
    vecs = embed(texts)
    keep, kept_vecs = [], []
    for i, v in enumerate(vecs):
        if v is None:
            keep.append(i)
            continue
        if all(cosine(v, kv) < threshold for kv in kept_vecs):
            keep.append(i)
            kept_vecs.append(v)
    return keep
