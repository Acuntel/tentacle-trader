"""The analyst — confirmation by REASONING, not source-counting.

Reads the dossier and judges: is this a REAL, FRESH, UP-direction catalyst, with independent evidence
and no contradiction from the options tape or price action? With ANTHROPIC_API_KEY set it uses Claude
(gated to the top few candidates per cycle — opt-in, bounded cost); otherwise a deterministic heuristic
with the SAME output shape, for free. Either way the verdict can only VETO or down/up-weight a setup —
it never touches the price-based mastery ledger (keeps the anti-hallucination guarantee intact).
"""
from __future__ import annotations

import json
import os

try:
    import anthropic
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "real": {"type": "boolean"},
        "direction": {"type": "string", "enum": ["up", "down", "unclear"]},
        "conviction": {"type": "integer"},
        "fresh": {"type": "boolean"},
        "bear_case": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["real", "direction", "conviction", "fresh", "bear_case", "rationale"],
    "additionalProperties": False,
}

SYSTEM = ("You are a skeptical event-driven equity analyst vetting ONE long setup. Confirm real=true "
          "ONLY for a fresh, material, UP-direction catalyst backed by INDEPENDENT evidence types "
          "(a filing/news event PLUS price/options/fundamentals agreeing) — not one echoed story. "
          "Default real=false if the evidence is thin, stale, or the options tape / price action "
          "contradicts the thesis. Be terse and decisive.")


class Analyst:
    def __init__(self, cfg: dict):
        a = cfg.get("analyst", {})
        self.on = bool(a.get("enabled", True))
        self.model = a.get("model", "claude-opus-4-8")
        self.max_llm = int(a.get("max_llm_per_cycle", 3))
        self.min_pillars = int(a.get("min_independent_pillars", 2))
        self._client = None
        self._used = 0
        if _HAVE and os.getenv("ANTHROPIC_API_KEY"):
            try:
                self._client = anthropic.Anthropic()
            except Exception:
                self._client = None

    @property
    def llm_live(self) -> bool:
        return self._client is not None

    def reset_cycle(self):
        self._used = 0

    def judge(self, dossier: dict) -> dict:
        if not self.on:
            return {"real": True, "direction": "up", "conviction": 60, "fresh": True,
                    "bear_case": "analyst disabled", "rationale": "analyst off"}
        if self._client and self._used < self.max_llm:
            v = self._llm(dossier)
            if v:
                self._used += 1
                v["by"] = "claude"
                return v
        return self._heuristic(dossier)

    def _heuristic(self, d: dict) -> dict:
        tech = d.get("tech") or {}
        opt = d.get("options") or {}
        indep = d["independence"]
        bear = []
        if opt.get("bias", 0) <= -0.35:
            bear.append("heavy put positioning into the move")
        if tech.get("bear"):
            bear.append("bearish candle: " + ", ".join(tech["bear"][:1]))
        direction = "up"
        if opt.get("bias", 0) <= -0.35 or (tech.get("trend") == "down" and tech.get("bias", 0) < 0):
            direction = "unclear"
        conv = min(100, 40 + 18 * indep + (10 if opt.get("bias", 0) >= 0.25 else 0))
        if direction != "up":
            conv = int(conv * 0.6)
        return {"real": (indep >= self.min_pillars and direction == "up"), "direction": direction,
                "conviction": int(conv), "fresh": True, "by": "heuristic",
                "bear_case": "; ".join(bear) or "none obvious",
                "rationale": f"{indep} orthogonal pillars ({', '.join(d['pillars'])}); "
                             f"{d['news_events']} distinct news events; "
                             f"options bias {opt.get('bias')}, price bias {tech.get('bias')}"}

    def _llm(self, d: dict) -> dict | None:
        try:
            r = self._client.messages.create(
                model=self.model, max_tokens=1024, system=SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
                messages=[{"role": "user", "content": "Judge this setup:\n" + json.dumps(d, default=str)}],
            )
            txt = next((b.text for b in r.content if b.type == "text"), "")
            return json.loads(txt) if txt else None
        except Exception:
            return None
