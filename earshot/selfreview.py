"""Self-review — the bot diagnoses its OWN behaviour and recommends fixes, continuously.

Every recommendation is grounded in a real counted fact from the immutable ledgers (open positions,
outcomes.jsonl, knowledge.json, the Dungeon). Nothing is invented: if there isn't enough data the
finding says so. Safe, bounded fixes (nudge a losing source's weight, trim a losing catalyst's prior)
are auto-applied; structural fixes (needs a code change) are flagged for the human. Written to
state/self_review.jsonl and printed each run so progress is visible.
"""
from __future__ import annotations

import json
import os
import time

from .models import iso
from .patterns import field_rate
from . import market

REVIEW_PATH = "state/self_review.jsonl"

# market.can_open() began hard-refusing off-hours entries at this moment (commit 0f1a244,
# 2026-06-20 15:48 ET). Ledger rows from BEFORE the gate existed (the three 2026-06-19 Juneteenth
# entries) are immutable history, not a live regression — rescanning the whole ledger re-raised
# them as a fresh sev-high alarm on every run, forever. Only post-fix entries can re-fire it.
MARKET_CLOCK_FIX_TS = 1781988537.0


def _read_jsonl(path, cap=2000):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:
        return []
    return out[-cap:]


class SelfReview:
    def __init__(self, cfg: dict, learner, instinct):
        self.cfg = cfg.get("selfreview", {})
        self.full_cfg = cfg
        self.learner = learner
        self.instinct = instinct
        self.min_n = int(self.cfg.get("min_samples", 5))

    def run(self) -> dict:
        findings: list[dict] = []
        know = self.learner.know
        outcomes = _read_jsonl("state/outcomes.jsonl")

        # 1) PROCESS ERROR — positions opened while the market was closed (the Juneteenth bug).
        # Currently-open positions are always checked; historical ledger rows only count if they
        # were entered AFTER the market-clock fix shipped (a re-fire here means a REAL regression).
        closed_open = [p for p in self.learner.open if market.was_closed(p.get("entry_ts", time.time()))]
        closed_hist = [o for o in outcomes if o.get("entry_ts", 0) >= MARKET_CLOCK_FIX_TS
                       and market.was_closed(o["entry_ts"])]
        if closed_open or closed_hist:
            findings.append({
                "issue": "entries placed while the market was closed",
                "evidence": f"{len(closed_open)} open + {len(closed_hist)} historical closed-market entries",
                "severity": "high", "status": "GUARDED",
                "fix": "market.can_open() now hard-refuses entries off-hours; these were logged at the "
                       "last close, not a tradeable price. Lesson recorded; will not recur.",
                "auto_applied": True})
            self._remember("Never open a position when the exchange is closed — the price isn't tradeable.")

        # 2) Per-catalyst expectancy — trim the prior on a proven money-loser (bounded auto-fix).
        for cat, st in (know.get("catalysts") or {}).items():
            n = st.get("trades", 0)
            if n >= self.min_n:
                avg = st.get("sum_ret", 0.0) / n
                if avg < 0:
                    applied = self._trim_prior(cat)
                    findings.append({
                        "issue": f"catalyst '{cat}' has negative expectancy",
                        "evidence": f"avg {avg:+.2f}%/trade over {n} resolved",
                        "severity": "medium", "status": "TUNED" if applied else "FLAGGED",
                        "fix": f"lower the '{cat}' prior" + (f" -> {applied}" if applied else ""),
                        "auto_applied": bool(applied)})

        # 3) Per-source precision — de-weight a source that keeps misleading it. Judged RELATIVE to the
        # field's alpha-win base rate (~0.34): "below the field by a clear margin", not an absolute bar
        # that half of all honest sources sit under (which ratcheted everything to the floor).
        fld = field_rate(know)
        for src, st in (know.get("sources") or {}).items():
            n = st.get("trades", 0)
            if n >= max(self.min_n, 8):
                prec = st.get("wins", 0) / n
                avg = st.get("sum_ret", 0.0) / n
                if prec < max(0.15, fld - 0.08) and avg <= 0:
                    new_w = self._deweight(src)
                    findings.append({
                        "issue": f"source '{src}' is low-precision",
                        "evidence": f"{prec*100:.0f}% win, {avg:+.2f}% avg over {n}",
                        "severity": "medium", "status": "TUNED",
                        "fix": f"weight -> {new_w}", "auto_applied": True})

        # 4) Earliness — are we chronically buying AFTER the move? (structural, flag for human)
        runups = [p.get("runup_pct") for p in self.learner.open if p.get("runup_pct") is not None]
        late = [r for r in runups if r > self.full_cfg["conviction"]["max_runup_pct"]]
        if runups and len(late) >= max(2, len(runups) // 2):
            findings.append({
                "issue": "many entries are late (price already ran)",
                "evidence": f"{len(late)}/{len(runups)} open positions entered above the earliness cap",
                "severity": "low", "status": "FLAGGED",
                "fix": "consider lowering conviction.max_runup_pct or weighting earlier tentacles higher",
                "auto_applied": False})

        # 4b) FAILING PARTS — Brady's law: "if a part is failing, honesty should know and then fix
        # it." A broken piece must never fail in silence: erroring tentacles, tentacles that produce
        # nothing, and dead LLM providers (which burn failover latency on every call) all get named.
        failing = []
        for src, hd in (know.get("tentacle_health") or {}).items():
            polls = hd.get("polls", 0)
            if polls >= 20 and hd.get("errors", 0) / max(1, polls) > 0.5:
                failing.append(f"tentacle {src}: {hd['errors']} errors/{polls} polls")
                findings.append({
                    "issue": f"tentacle '{src}' is FAILING (majority of polls error)",
                    "evidence": f"{hd['errors']} errors over {polls} polls this session",
                    "severity": "high", "status": "FLAGGED",
                    "fix": "fix the endpoint/key/parser or disable the tentacle in config",
                    "auto_applied": False})
            elif polls >= 300 and hd.get("signals", 0) == 0:
                failing.append(f"tentacle {src}: 0 signals in {polls} polls")
                findings.append({
                    "issue": f"tentacle '{src}' produces NOTHING (may be quietly broken)",
                    "evidence": f"0 signals over {polls} polls this session",
                    "severity": "medium", "status": "FLAGGED",
                    "fix": "verify the source still publishes and the parser still matches its format",
                    "auto_applied": False})
        try:
            stats = json.load(open("state/llm_stats.json"))
            agg: dict = {}
            for key, v in stats.items():
                name = key.split("|")[0]
                a = agg.setdefault(name, {"calls": 0, "ok": 0})
                a["calls"] += v.get("calls", 0)
                a["ok"] += v.get("ok", 0)
            for name, a in agg.items():
                if a["calls"] >= 50 and a["ok"] == 0:
                    failing.append(f"LLM {name}: 0/{a['calls']} usable answers")
                    findings.append({
                        "issue": f"LLM provider '{name}' is DEAD (never answers)",
                        "evidence": f"0 usable answers in {a['calls']} attempts",
                        "severity": "medium", "status": "FLAGGED",
                        "fix": "fix/rotate its key or remove it from llm.ROUTING — it burns failover latency",
                        "auto_applied": False})
        except Exception:
            pass
        know["failing_parts"] = failing                     # the daily digest names them to Brady

        # 5) Honest progress check — distance to the next rung (no inflation).
        stt = self.instinct.status()
        findings.append({
            "issue": "graduation status", "evidence": f"mastery {stt['mastery']} / need "
            f"{stt['needs_mastery']} for rung {stt['rung']} ({stt['stage']}); {len(outcomes)} resolved trades",
            "severity": "info", "status": "ON-TRACK" if outcomes else "NEEDS-RESOLVED-TRADES",
            "fix": "keep shadow trades resolving — mastery only moves on real price-based outcomes",
            "auto_applied": False})

        report = {"ts": iso(time.time()), "findings": findings,
                  "n_auto_applied": sum(1 for f in findings if f.get("auto_applied"))}
        os.makedirs(os.path.dirname(REVIEW_PATH) or ".", exist_ok=True)
        with open(REVIEW_PATH, "a") as f:
            f.write(json.dumps(report) + "\n")
        self.learner._persist_know()
        return report

    # ---- bounded auto-fixes (the bot may only nudge, never rewrite its own hard law) ----
    def _trim_prior(self, cat: str):
        c = self.full_cfg.get("catalysts", {}).get(cat)
        if not c:
            return None
        c["prior"] = round(max(0.5, c["prior"] * 0.95), 3)
        return c["prior"]

    def _deweight(self, src: str):
        w = self.learner.weights
        w[src] = round(max(0.1, w.get(src, 0.5) * 0.9), 3)
        return w[src]

    def _remember(self, lesson: str):
        lessons = self.learner.know.setdefault("lessons", [])
        if lesson not in lessons:
            lessons.append(lesson)

    @staticmethod
    def summarize(report: dict, top: int = 4) -> list[str]:
        rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
        ordered = sorted(report["findings"], key=lambda f: rank.get(f["severity"], 9))
        return [f"[{f['status']}] {f['issue']} — {f['evidence']}" for f in ordered[:top]]
