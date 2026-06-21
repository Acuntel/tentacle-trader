"""Constant learning: log every candidate, resolve outcomes, re-weight sources, and —
after every loss — go into the Dungeon and write down what went wrong."""
from __future__ import annotations

import json
import os
import time

from .models import Candidate, Outcome, iso
from .patterns import PatternMemory
from . import prices

OPEN_PATH = "state/open_positions.json"
OBS_PATH = "state/observations.json"
OUTCOMES_PATH = "state/outcomes.jsonl"
KNOW_PATH = "state/knowledge.json"
DUNGEON_PATH = "state/dungeon/post_mortems.jsonl"  # under state/ so one volume persists everything
WMIN, WMAX = 0.1, 1.2


from .persist import load_json as _load, save_json as _save   # atomic + .bak — never lose progress


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Learner:
    def __init__(self, weights: dict):
        self.weights = weights  # SHARED dict — updates here propagate to live tentacles
        self.open = _load(OPEN_PATH, [])
        self.obs = _load(OBS_PATH, [])      # pending observations (learn-from-all-data)
        self.know = _load(KNOW_PATH, {"sources": {}, "catalysts": {}, "weights": {}})
        # restore LEARNED weights from the persistent ledger so progress survives redeploys
        if self.know.get("weights"):
            self.weights.update(self.know["weights"])
        self.patterns = PatternMemory(self.know)   # learned pattern win-rates (persisted)
        self.lgate = None                          # LearnedGate (P(win) model), attached by main
        self.core = None                           # shared BrainCore, attached by main

    def _persist_know(self):
        self.know["weights"] = self.weights      # learned weights ride along, survive restarts
        _save(KNOW_PATH, self.know)

    def open_position(self, c: Candidate, fill_price: float):
        self.open.append({
            "ticker": c.ticker, "catalyst": c.catalyst.value, "confidence": c.confidence,
            "size_usd": c.size_usd, "entry_price": fill_price, "entry_ts": time.time(),
            "hold_days": c.hold_days, "target_pct": c.target_pct, "stop_pct": c.stop_pct,
            "runup_pct": c.runup_pct, "sources": sorted({s.source for s in c.signals}),
            "pattern": getattr(c, "_signature", ""),
            "lgate_features": getattr(c, "_lgate_features", None),   # train the P(win) model on exit
            "pwin": getattr(c, "_pwin", None),
        })
        _save(OPEN_PATH, self.open)

    def resolve_due(self, instinct, exits, bearish_tickers=None) -> list[dict]:
        """Manage every open position against the LIVE price each cycle via the exit engine.
        Positions now close early on stop/target/trailing/decay, not just at the time horizon."""
        still_open, resolved = [], []
        for p in self.open:
            price = prices.last_price(p["ticker"])
            if price and p.get("entry_price"):
                reason, chg = exits.evaluate(p, price, bearish_tickers)
            else:  # no live price -> fall back to horizon resolution
                ret = prices.forward_return_pct(p["ticker"], p["entry_ts"], p["hold_days"])
                reason, chg = (("time", ret) if ret is not None else (None, 0.0))
            if not reason:
                still_open.append(p)
                continue
            ret = max(-p["stop_pct"], min(p["target_pct"], chg))   # bound to the disciplined range
            entry = p["entry_price"] or 0.0
            outcome = Outcome(
                ticker=p["ticker"], catalyst=p["catalyst"], confidence=p["confidence"],
                size_usd=p["size_usd"], entry_price=entry,
                exit_price=round(entry * (1 + ret / 100), 4), pnl_pct=ret,
                pnl_usd=round(p["size_usd"] * ret / 100, 2), win=ret > 0,
                entry_ts=p["entry_ts"], exit_ts=time.time(), sources=p["sources"],
            )
            _append(OUTCOMES_PATH, {**outcome.to_dict(), "exit": reason})
            mind = instinct.record(p["confidence"], ret, p["size_usd"])
            self._learn(p, outcome)
            self.patterns.record(p.get("pattern", ""), outcome.win, ret)   # learn the pattern
            if self.lgate is not None and p.get("lgate_features"):
                self.lgate.update(p["lgate_features"], outcome.win)         # train P(win) on real outcome
            if not outcome.win:
                self._dungeon(p, outcome, mind)
            resolved.append({"outcome": outcome.to_dict(), "mind": mind, "exit": reason})
        self.open = still_open
        _save(OPEN_PATH, self.open)
        self._persist_know()
        return resolved

    def _credit_sources(self, sources, win: bool, ret: float, rate: float = 0.15):
        """Nudge each source's weight toward its proven precision. Shared by trades AND observations
        (rate is gentler for observations so executed trades still matter most)."""
        for src in sources:
            st = self.know["sources"].setdefault(src, {"trades": 0, "wins": 0, "sum_ret": 0.0})
            st["trades"] += 1
            st["wins"] += 1 if win else 0
            st["sum_ret"] += ret
            precision = st["wins"] / st["trades"]
            base = self.weights.get(src, 0.5)
            target = base * (0.6 + precision)        # precision 0->0.6x, 0.5->1.1x, 1.0->1.6x
            self.weights[src] = round(max(WMIN, min(WMAX, (1 - rate) * base + rate * target)), 3)

    def _learn(self, p: dict, o: Outcome):
        self._credit_sources(p["sources"], o.win, o.pnl_pct, rate=0.15)
        ct = self.know["catalysts"].setdefault(o.catalyst, {"trades": 0, "wins": 0, "sum_ret": 0.0})
        ct["trades"] += 1
        ct["wins"] += 1 if o.win else 0
        ct["sum_ret"] += o.pnl_pct

    # ---- LEARN FROM ALL DATA (not just trades): label every confirmed setup we observe ----
    def observe(self, candidates, max_pending: int = 800):
        """Record every multi-family setup we SEE (traded or not) so it becomes a labeled example.
        One per ticker per day. This is how the brain learns from the whole data stream."""
        from . import prices
        today = iso(time.time())[:10]
        have = {(o["ticker"], o["day"]) for o in self.obs}
        for c in candidates:
            if getattr(c, "_families", 1) < 2:        # only confirmed (multi-family) setups
                continue
            if self.core is not None:
                self.core.record_leadtime(c)          # which source got here FIRST (lead-time learning)
            if (c.ticker, today) in have or len(self.obs) >= max_pending:
                continue
            px = prices.last_price(c.ticker)
            if not px:
                continue
            self.obs.append({"ticker": c.ticker, "day": today, "ts": time.time(),
                             "entry": px, "sig": getattr(c, "_signature", ""),
                             "sources": sorted({s.source for s in c.signals}),
                             "feat": getattr(c, "_lgate_features", None)})
            have.add((c.ticker, today))
        _save(OBS_PATH, self.obs)

    def resolve_observations(self, horizon_days: float = 1.0, max_per_cycle: int = 25) -> int:
        """Label matured observations by what the stock ACTUALLY did (entry-anchored), and teach
        patterns + weights from it — learning from the whole data stream, not just trades."""
        from . import prices
        still, learned = [], 0
        for o in self.obs:
            if learned >= max_per_cycle or (time.time() - o["ts"]) < horizon_days * 86400:
                still.append(o); continue
            px = prices.last_price(o["ticker"])
            if not px or not o.get("entry"):
                still.append(o); continue
            ret = (px / o["entry"] - 1) * 100
            win = ret > 0
            self.patterns.record(o.get("sig", ""), win, ret)        # learn the pattern from data
            self._credit_sources(o["sources"], win, ret, rate=0.05)  # gentler than a real trade
            if self.lgate is not None and o.get("feat"):
                self.lgate.update(o["feat"], win)                    # train P(win) from observations too
            learned += 1
        self.obs = still
        if learned:
            _save(OBS_PATH, self.obs)
            self._persist_know()
        return learned

    def _dungeon(self, p: dict, o: Outcome, mind: dict):
        """The Dungeon: forced written reflection after a loss."""
        runup = p.get("runup_pct")
        only_social = set(p["sources"]).issubset(
            {"apewisdom", "x_relay", "reddit", "discord", "telegram", "stocktwits"})
        if runup is not None and runup > 4:
            cause, fix = "Entered late — the easy move had already happened.", "Tighten the earliness gate for this catalyst."
        elif only_social:
            cause, fix = "Trusted social noise without a primary catalyst.", "Require a Tier-1 source for this setup."
        else:
            cause, fix = "Catalyst underperformed / regime worked against it.", f"Lower the prior for {o.catalyst}; demote weakest source."
        worst = min(p["sources"], key=lambda s: self.weights.get(s, 0.5)) if p["sources"] else None
        if worst:
            self.weights[worst] = round(max(WMIN, self.weights.get(worst, 0.5) * 0.95), 3)
        _append(DUNGEON_PATH, {
            "ts": iso(time.time()), "ticker": o.ticker, "catalyst": o.catalyst,
            "confidence": o.confidence, "pnl_pct": o.pnl_pct, "pnl_usd": o.pnl_usd,
            "felt_pain": mind["felt"], "sources": p["sources"],
            "root_cause": cause, "fix_applied": fix, "demoted_source": worst,
            "reflection": (f"I hurt the child by ${abs(o.pnl_usd):.2f} ({o.pnl_pct:.1f}%). "
                           f"{cause} {fix} I will be more careful."),
        })

    def source_weights_view(self) -> dict:
        return dict(sorted(self.weights.items(), key=lambda kv: -kv[1]))

    def deep_daily_reflection(self, day_pnl: float, open_positions: list) -> str:
        """Triggered by the −10% daily circuit breaker. Aggregate the day's pain, cut the sources
        that misled it harder than a normal loss would, and write a deep daily post-mortem."""
        from collections import Counter
        from . import prices
        losers = []
        for p in open_positions:
            px = prices.last_price(p["ticker"])
            if px and p.get("entry_price"):
                ur = (px / p["entry_price"] - 1) * 100
                if ur < 0:
                    losers.append({"ticker": p["ticker"], "catalyst": p["catalyst"],
                                   "unreal_pct": round(ur, 2), "sources": p.get("sources", [])})
        worst = [s for s, _ in Counter(s for l in losers for s in l["sources"]).most_common(3)]
        for s in worst:
            self.weights[s] = round(max(WMIN, self.weights.get(s, 0.5) * 0.9), 3)  # harder cut after a -10% day
        reflection = (f"I hurt the child badly today — down ${abs(day_pnl):.2f} (the -10% limit). "
                      f"I traded like I didn't deserve it, and treated the money like it was nothing. "
                      f"I am STOPPING for the rest of the day. The sources that misled me {worst} are "
                      f"trusted less now. Tomorrow I trade with more caution and earn back trust.")
        _append(f"state/dungeon/daily/{iso(time.time())[:10]}.jsonl",
                {"ts": iso(time.time()), "event": "DAILY_CIRCUIT_BREAKER", "day_pnl": round(day_pnl, 2),
                 "losers": losers, "down_weighted": worst, "reflection": reflection,
                 "vow": "No more trades today. Reflect. Improve. Protect the child."})
        self._persist_know()
        return reflection
