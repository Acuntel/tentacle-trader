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
        closed_open = [p for p in self.learner.open if market.was_closed(p.get("entry_ts", time.time()))]
        closed_hist = [o for o in outcomes if market.was_closed(o.get("entry_ts", 0))]
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
