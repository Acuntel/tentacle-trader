"""Semantic memory — given the current setup, recall what happened in SIMILAR past setups.

PatternMemory already tracks win-rates for the EXACT signature. This adds generalization: a k-NN over
bge-m3 embeddings of the labeled-setup corpus, so a rare or brand-new signature borrows experience from
semantically-similar ones ("setups like this historically returned X% / won Y%"). Embeddings are cached
per unique signature (low-cardinality) so it's nearly free after warmup. Advisory — it INFORMS the
confidence, it never overrides the gate. Fails open (returns None) without an embedding key.
"""
from __future__ import annotations

import json
import time

from . import embed

RESOLVED = "state/resolved_obs.jsonl"


class SemanticMemory:
    def __init__(self, cfg=None):
        c = (cfg or {}).get("memory", {}) if isinstance(cfg, dict) else {}
        self.k = int(c.get("k", 25))
        self.min_corpus = int(c.get("min_corpus", 60))
        self.cap = int(c.get("corpus_cap", 1200))
        self.refresh = int(c.get("refresh_min", 60)) * 60
        self._rows = None
        self._vecs = None
        self._built = 0.0

    def available(self) -> bool:
        return embed.available()

    def _build(self):
        rows = []
        try:
            with open(RESOLVED) as f:
                for line in f:
                    try:
                        o = json.loads(line)
                        sig, ret = o.get("sig"), o.get("ret")
                        if sig and ret is not None:
                            r = float(ret)
                            if r == r:                              # finite
                                rows.append((sig, r, bool(o.get("win", r > 0))))
                    except Exception:
                        continue
        except FileNotFoundError:
            pass
        rows = rows[-self.cap:]
        self._rows = rows
        self._vecs = embed.embed([r[0] for r in rows], max_texts=self.cap) if rows else []
        self._built = time.time()

    def _maybe_refresh(self):
        if self._rows is None or (time.time() - self._built) > self.refresh:
            self._build()

    def recall(self, sig: str):
        """Return {n, mean_ret, win_rate} over the k most semantically-similar past setups, or None."""
        if not (embed.available() and sig):
            return None
        if self._rows is None or (time.time() - self._built) > self.refresh:
            self._build()
        if len(self._rows) < self.min_corpus:
            return None
        qv = embed.embed([sig])[0]
        if qv is None:
            return None
        sims = [(embed.cosine(qv, v), i) for i, v in enumerate(self._vecs) if v is not None]
        if not sims:
            return None
        sims.sort(reverse=True)
        top = [self._rows[i] for _, i in sims[:self.k]]
        n = len(top)
        rets = [r[1] for r in top]
        wins = sum(1 for r in top if r[2])
        return {"n": n, "mean_ret": round(sum(rets) / n, 3), "win_rate": round(wins / n, 3)}
