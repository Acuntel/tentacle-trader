"""Distillation — the bot learns to MIMIC its expensive LLM analyst on cheap CPU.

Every time the LLM judges a setup, we record (features -> the LLM's conviction). A tiny online regressor
learns to reproduce that judgment from features alone. So when the LLM is unavailable (no key, rate-limited,
or over its daily budget), the cheap path is a LEARNED approximation of the teacher instead of a dumb
heuristic — and over time the bot leans on the LLM less. Persisted in knowledge.json; tracks FIDELITY
(how closely it reproduces the teacher). It mimics the analyst; the separate P(win) model still learns from
real outcomes — together they're a teacher (LLM) distilled into a student, validated by reality.
"""
from __future__ import annotations

import math


class DistilledJudge:
    def __init__(self, know: dict, lr: float = 0.03, key: str = "distill"):
        self.know = know
        self.key = key
        m = know.get(key, {})
        self.w = dict(m.get("w", {}))
        self.b = float(m.get("b", 0.0))
        self.n = int(m.get("n", 0))
        self.mse = float(m.get("mse", 0.08))               # EMA squared error vs the teacher (0..1 scale)
        self.lr = lr

    def predict(self, feats: dict) -> float:
        """Predicted conviction, 0-100, in the LLM analyst's units."""
        z = self.b + sum(self.w.get(k, 0.0) * v for k, v in feats.items())
        return 100.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def record(self, feats: dict, verdict: dict):
        """Learn from one teacher judgment: target = the LLM's conviction (0-1)."""
        target = max(0.0, min(1.0, float(verdict.get("conviction", 50)) / 100.0))
        pred = self.predict(feats) / 100.0
        err = target - pred
        for k, v in feats.items():
            self.w[k] = self.w.get(k, 0.0) + self.lr * err * v
        self.b += self.lr * err
        self.n += 1
        self.mse = 0.99 * self.mse + 0.01 * err * err
        self.know[self.key] = {"w": self.w, "b": round(self.b, 4), "n": self.n, "mse": round(self.mse, 5)}

    def ready(self, min_n: int = 50) -> bool:
        return self.n >= min_n

    def fidelity(self) -> float:
        """0..1 — how closely the student reproduces the teacher (1 = near-perfect mimicry)."""
        return max(0.0, min(1.0, 1.0 - self.mse * 6.0))
