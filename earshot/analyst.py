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

from . import llm

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
    """Combination judge: the rule-based heuristic AND a free LLM (Groq) BOTH score every setup,
    and their verdicts are FUSED — not a fallback. The heuristic votes on every candidate; the LLM
    votes on the top few (bounded). When both speak, firing requires CONSENSUS (both must call it
    real and agree on direction), which is a stronger gate than either alone. The heuristic also
    guarantees the bot never goes blind if the free brain blips. Anthropic stays optional + OFF
    (provider != 'anthropic') so the running cost is $0."""

    def __init__(self, cfg: dict):
        a = cfg.get("analyst", {})
        self.on = bool(a.get("enabled", True))
        self.provider = a.get("provider", "groq")          # groq (free) | anthropic (paid) | heuristic
        self.model = a.get("model", "claude-opus-4-8")     # only used if provider == anthropic
        self.max_llm = int(a.get("max_llm_per_cycle", 3))
        self.min_pillars = int(a.get("min_independent_pillars", 2))
        self._client = None
        self._used = 0
        if self.provider == "anthropic" and _HAVE and os.getenv("ANTHROPIC_API_KEY"):
            try:
                self._client = anthropic.Anthropic()
            except Exception:
                self._client = None
        self._groq = (self.provider not in ("anthropic", "heuristic")) and llm.have_brain()

    @property
    def llm_live(self) -> bool:
        return self._client is not None or self._groq

    def brain_name(self) -> str:
        if self._groq and self.on:
            return f"llm+heuristic (combined, {llm.provider()})"
        if self._client and self.on:
            return "claude+heuristic (combined, paid)"
        return "heuristic (free; set GROQ_API_KEY for the free LLM, or XAI_API_KEY for paid Grok)"

    def reset_cycle(self):
        self._used = 0

    def judge(self, dossier: dict) -> dict:
        if not self.on:
            return {"real": True, "direction": "up", "conviction": 60, "fresh": True,
                    "bear_case": "analyst disabled", "rationale": "analyst off"}
        h = self._heuristic(dossier)                        # the rules ALWAYS vote
        v = None
        if self._used < self.max_llm:
            if self._groq:
                v = self._llm_groq(dossier)
            elif self._client:
                v = self._llm(dossier)
        if not v:
            return h                                          # rules carry it (brain offline this call)
        self._used += 1
        return self._combine(h, v)

    def _combine(self, h: dict, l: dict) -> dict:
        """Fuse the rule verdict and the LLM verdict — consensus to fire, blended conviction."""
        agree_dir = h["direction"] if h["direction"] == l.get("direction") else "unclear"
        real = bool(h["real"] and l.get("real") and agree_dir == "up")
        conv = round(0.5 * h["conviction"] + 0.5 * int(l.get("conviction", 0)))
        if not real:
            conv = int(conv * 0.6)
        bears = [b for b in (h.get("bear_case"), l.get("bear_case")) if b and b != "none obvious"]
        return {"real": real, "direction": agree_dir, "conviction": int(conv),
                "fresh": bool(h.get("fresh", True) and l.get("fresh", True)), "by": "groq+heuristic",
                "bear_case": "; ".join(dict.fromkeys(bears)) or "none obvious",
                "rationale": f"COMBINED — rules: {h['rationale']} || llm: {l.get('rationale', '')}"}

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

    def _llm_groq(self, d: dict) -> dict | None:
        """Free Groq verdict. Same shape as the heuristic so _combine can fuse them."""
        user = ("Judge this ONE long setup and reply as a JSON object with EXACTLY these keys: "
                'real (bool), direction ("up"/"down"/"unclear"), conviction (int 0-100), '
                "fresh (bool), bear_case (string), rationale (string, one terse sentence).\n\n"
                + json.dumps(d, default=str))
        v = llm.ask_json(SYSTEM, user, max_tokens=512)
        if not v or "real" not in v or "direction" not in v:
            return None
        v["by"] = "groq"
        return v

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
