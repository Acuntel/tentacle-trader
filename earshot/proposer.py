"""Daily proposal engine — "load up all the ideas, rank them, send only the best ~12/day."

Each coach pass it HARVESTS candidate self-improvements — config-threshold tweaks scored by the LLM
over the whole-brain snapshot, plus the occasional gate-ready code patch — and merges them into a
persistent, de-duped POOL with an impact score (0-100). It only refills the pool when it's running low
(saves LLM calls). Then EMIT_DUE sends the single highest-impact un-sent candidate as a ✅/❌ card,
respecting the daily budget + pacing in ApprovalQueue. So Brady gets a steady trickle of the BEST ideas
to approve, not spam and not silence.

Only SAFE, reversible knobs are tunable here. The hard risk caps, the real-money gate, and the Gauntlet
are NEVER in the menu, and every emitted change still goes through the test-gate before it can ship.
"""
from __future__ import annotations

import json
import re

from . import llm, selfcode

CONFIG_PATH = "config.yaml"

# leaf_key : (low, high) allowed range. Only reversible trading knobs — never a risk rail.
TUNABLE = {
    "min_pwin": (0.40, 0.65),
    "min_score_to_trade": (10, 40),   # AUDIT FIX: was (45,70) — Brady deliberately runs 15; the old
                                       # range let the autonomous tuner snap the bar back to 45+ in one step
    "max_runup_pct": (3.0, 8.0),
    "regime_downtrend_premium": (5, 25),
    "min_families": (2, 3),
    "min_independent_sources": (2, 3),
    "trail_pct": (3.0, 9.0),
    "trail_arm_pct": (2.0, 6.0),
    "max_open_positions": (4, 15),
    "walk_forward_folds": (3, 8),
    "min_fold_winrate": (0.40, 0.70),
}

PRETTY = {
    "min_pwin": "minimum win-chance I need before I'll trade",
    "min_score_to_trade": "conviction bar I need to trade",
    "max_runup_pct": "how far a stock can already have moved before I skip it",
    "regime_downtrend_premium": "extra caution when the market's falling",
    "min_families": "number of different signal types I need to agree",
    "min_independent_sources": "number of independent sources I need to agree",
    "trail_pct": "profit I give back before I lock in a win",
    "trail_arm_pct": "gain needed before I start protecting profit",
    "max_open_positions": "how many positions I hold at once",
    "walk_forward_folds": "how many time periods I test a strategy across",
    "min_fold_winrate": "how consistent a strategy must be to trust it",
}

SYS = ("You tune an autonomous, still-PRACTICE-mode trading bot. Given its brain (real measured state) "
       "and a list of SAFE, reversible threshold settings (current value + allowed range), propose ONLY "
       "changes you can directly justify from the SPECIFIC evidence shown — what the data says is "
       "actually wrong right now. QUALITY OVER QUANTITY: if nothing is clearly worth changing, return an "
       "EMPTY candidates list. Never propose a cosmetic, speculative, or 'might help' tweak. You may NOT "
       "touch risk caps, the money gate, or the ladder (not in the list). Reply ONLY JSON: "
       "{\"candidates\":[{\"key\":\"min_pwin\",\"proposed\":0.55,\"impact\":0-100,\"plain\":\"<plain "
       "English what it does>\",\"why\":\"<the SPECIFIC evidence that justifies it — cite the numbers>\"}]}. "
       "impact = how strongly the evidence supports it; proposed must differ from current and stay in "
       "range; only use keys from the list.")


class Proposer:
    def __init__(self, cfg, know, approvals):
        self.cfg = cfg
        self.know = know
        self.ap = approvals
        self.pool = know.setdefault("proposal_pool", [])   # [{hash,title,patch,plain,why,tech,impact,sent}]
        self.refill_below = int(cfg.get("telegram", {}).get("pool_refill_below", 12))
        self.min_impact = int(cfg.get("telegram", {}).get("min_impact", 60))   # HIGH-confidence bar
        self.floor = int(cfg.get("telegram", {}).get("min_impact_floor", 25))  # absolute floor to ever send
        # loosened gate: when nothing clears min_impact, still send the single best idea, flagged low-conf
        self.always_send_best = bool(cfg.get("telegram", {}).get("always_send_best", True))

    # ---- helpers ----
    def _text(self) -> str:
        try:
            return open(CONFIG_PATH).read()
        except Exception:
            return ""

    @staticmethod
    def _current_str(text: str, key: str):
        m = re.search(rf"(?m)^\s*{re.escape(key)}:\s*([^\s#]+)", text)
        return m.group(1) if m else None

    @staticmethod
    def _fmt(cur_str: str, val) -> str:
        if "." in cur_str:                                  # match the current value's decimal places
            dec = len(cur_str.split(".", 1)[1])
            return f"{float(val):.{dec}f}"
        return str(int(round(float(val))))

    def _unsent(self):
        denied = self.know.get("approvals", {}).get("denied", {})
        return [p for p in self.pool if not p.get("sent") and p["hash"] not in denied and p.get("impact", 0) >= self.floor]

    def _add(self, item: dict):
        if any(p["hash"] == item["hash"] for p in self.pool):
            return
        if item["hash"] in self.know.get("approvals", {}).get("denied", {}):
            return
        self.pool.append(item)
        self.know["proposal_pool"] = self.pool[-200:]       # bounded backlog

    # ---- harvest CONFIG tweak candidates (only when the pool is running low) ----
    def harvest(self, snapshot: dict) -> int:
        if len(self._unsent()) >= self.refill_below or not llm.have_free_brain() or not TUNABLE:
            return 0
        text = self._text()
        menu = {}
        for key, (lo, hi) in TUNABLE.items():
            cur = self._current_str(text, key)
            if cur is not None and text.count(f"{key}: {cur}") == 1:   # must be a unique, applyable anchor
                menu[key] = {"current": cur, "min": lo, "max": hi}
        if not menu:
            return 0
        v = llm.ask_json(SYS, "Brain:\n" + json.dumps(snapshot, default=str)[:4000] +
                         "\n\nTunable settings:\n" + json.dumps(menu),
                         max_tokens=900, priority="high", task="propose")
        added = 0
        for c in ((v or {}).get("candidates") or [])[:8]:
            key = str(c.get("key", ""))
            if key not in menu:
                continue
            cur = menu[key]["current"]
            try:
                prop = self._fmt(cur, c.get("proposed"))
                if prop == cur or not (TUNABLE[key][0] <= float(prop) <= TUNABLE[key][1]):
                    continue
            except Exception:
                continue
            imp = int(max(0, min(100, c.get("impact", 50))))
            why = str(c.get("why", "")).strip()
            if imp < self.floor or len(why) < 15:          # keep low-conf ideas (labeled when sent), drop only junk
                continue
            find = f"{key}: {cur}"
            h = f"{key}={prop}"
            plain = c.get("plain") or f"Change my {PRETTY.get(key, key)} from {cur} to {prop}."
            self._add({"hash": h, "title": f"config: {key} {cur} → {prop}",
                       "patch": [{"file": "config.yaml", "find": find, "replace": f"{key}: {prop}"}],
                       "plain": plain, "why": why, "tech": f"config.yaml · {key}: {cur} → {prop}",
                       "impact": imp, "sent": False})
            added += 1
        return added

    # ---- a config-threshold change discovered by the Forge (deterministic search) ----
    def add_config(self, key: str, value, plain: str, why: str, impact: int = 60) -> bool:
        text = self._text()
        cur = self._current_str(text, key)
        if cur is None or str(value) == str(cur) or text.count(f"{key}: {cur}") != 1:
            return False                                    # need a unique, applyable anchor
        self._add({"hash": f"{key}={value}", "title": f"config: {key} {cur} → {value}",
                   "patch": [{"file": "config.yaml", "find": f"{key}: {cur}", "replace": f"{key}: {value}"}],
                   "plain": plain, "why": why, "tech": f"config.yaml · {key}: {cur} → {value}",
                   "impact": int(impact), "sent": False})
        return True

    # ---- a gate-ready code patch is an idea too (unbounded novelty) ----
    def add_code(self, patch: dict, impact: int = 55):
        import os
        fname = os.path.basename(str(patch.get("file", "")))
        why = str(patch.get("why", "a small improvement to my own code"))
        h = "code:" + str(abs(hash((patch.get("file"), patch.get("find"), patch.get("replace")))))[:10]
        self._add({"hash": h, "title": f"code change in {fname}",
                   "patch": [{"file": patch["file"], "find": patch["find"], "replace": patch["replace"]}],
                   "plain": f"Improve my own code — {why}",
                   "why": "It already passed my safety test-run, and I auto-revert it if it ever breaks anything.",
                   "tech": f"edits {patch['file']}", "impact": int(impact), "sent": False})

    # ---- emit the single best un-sent idea, if the budget + pacing allow ----
    def emit_due(self):
        if self.ap is None or not self.ap._budget_ok():
            return None
        for p in sorted(self._unsent(), key=lambda x: -x.get("impact", 0)):
            imp = p.get("impact", 0)
            if imp < self.floor:                           # sorted desc -> nothing else clears even the floor
                break
            low_conf = imp < self.min_impact
            if low_conf and not self.always_send_best:     # gate kept strict -> stay silent on below-bar ideas
                break
            title = ("⚠️ low-confidence — " + p["title"]) if low_conf else p["title"]
            plain = (("⚠️ Low confidence (this didn't clear my usual bar, but you asked to see my best idea "
                      "anyway): " + p["plain"]) if low_conf else p["plain"])
            # W3.1: pass the REAL kind through — this used to hard-code "config" for EVERY pool item,
            # so all recorded self_changes (code patches included) landed as kind:"config" and the
            # coach's verdicts could never attribute them.
            kind = ("code" if any(str(f.get("file", "")).endswith(".py") for f in (p.get("patch") or []))
                    else "config")
            cid = self.ap.propose(kind, title, p["why"], patches=p["patch"],
                                  meta={"plain": plain, "why": p["why"], "tech": p["tech"],
                                        "impact": imp, "low_conf": low_conf})
            # AUDIT FIX: propose() returns None for TEMPORARY blocks too (knob cooldown, pacing,
            # notify-off) — unconditionally marking sent silently discarded those ideas forever.
            # Final states (accepted / true dup / previously denied) mark sent; temporary blocks
            # leave the idea queued for a later pass.
            from .approvals import _hash as _ap_hash
            h = _ap_hash(kind, title, p["patch"])
            if cid is not None or h in self.ap.st.get("seen", {}) or h in self.ap.st.get("denied", {}):
                p["sent"] = True
            self.know["proposal_pool"] = self.pool
            return cid
        return None
