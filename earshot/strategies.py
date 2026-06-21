"""Multi-strategy tournament — many strategies shadow-trade the SAME decade of history (and, through
the live brain, the same live tape) at once, competing on a leaderboard. Each strategy is a parameter
vector with a distinct STYLE (momentum / breakout / pullback) and its own entry threshold, stop, target,
and hold. Every heartbeat they all trade the same sampled events (a fair head-to-head), their stats
accrue in the shared brain, and the CHAMPION's rules are adopted as the live strategy. So the bot is
always running an experiment on what actually works and folding the winner back into how it really trades.

`count` is configurable (5 / 10 / 100 / 1000). Honest note: past a few dozen the parameter space starts
repeating — more strategies = more compute, not automatically more edge. The leaderboard is the truth.
"""
from __future__ import annotations

import random
import statistics

from . import history

STYLES = ("momentum", "breakout", "pullback")


class StrategyBook:
    def __init__(self, cfg: dict, core):
        s = cfg.get("strategies", {})
        self.on = bool(s.get("enabled", True))
        self.count = int(s.get("count", 24))
        self.per_cycle_events = int(s.get("events_per_cycle", 200))
        self.min_trades = int(s.get("min_trades_to_rank", 40))
        self.lam = float(cfg["survival"]["loss_aversion_lambda"])
        self.core = core
        self.stats = core.know.setdefault("strategies", {})   # id -> {trades, wins, sum_ret, util, def}
        self.book = self._generate(self.count)

    def _generate(self, n: int) -> list[dict]:
        grid = []
        for style in STYLES:
            for mn in (3, 5, 8):
                for stop in (6, 8, 10):
                    for target in (10, 15, 20):
                        for hold in (3, 5, 10):
                            grid.append({"style": style, "min_move": mn, "stop": stop,
                                         "target": target, "hold": hold, "uptrend": style != "pullback"})
        random.shuffle(grid)
        return [{"id": f"S{i:04d}", **grid[i % len(grid)]} for i in range(n)]

    def _trade(self, s: dict, closes: list[float], i: int):
        if i < 252 or i + 1 >= len(closes) or closes[i - 1] <= 0:
            return None
        sma50 = sum(closes[i - 50:i]) / 50
        up = closes[i] > sma50
        if s["uptrend"] and not up:
            return None
        if s["style"] == "momentum":
            if (closes[i] / closes[i - 1] - 1) * 100 < s["min_move"]:
                return None
        elif s["style"] == "breakout":
            if closes[i] < max(closes[i - 252:i]):
                return None
        elif s["style"] == "pullback":
            if (closes[i] / closes[i - 1] - 1) * 100 > -s["min_move"]:   # need a real dip in an uptrend
                return None
        entry = closes[i]
        ret = 0.0
        for j in range(i + 1, min(i + 1 + s["hold"], len(closes))):
            chg = (closes[j] / entry - 1) * 100
            if chg <= -s["stop"]:
                return -float(s["stop"])
            if chg >= s["target"]:
                return float(s["target"])
            ret = chg
        return round(ret, 3)

    def evaluate(self) -> dict:
        if not self.on:
            return {"ran": 0}
        tickers = history.cached_tickers()
        if len(tickers) < 5:
            return {"ran": 0, "note": "history loading"}
        evs = []                                              # one shared event set -> a FAIR comparison
        for _ in range(self.per_cycle_events * 5):
            if len(evs) >= self.per_cycle_events:
                break
            bars = history.load(random.choice(tickers))
            if len(bars) < 260:
                continue
            closes = [c for _, c in bars]
            evs.append((closes, random.randint(252, len(closes) - 11)))
        ran = 0
        for strat in self.book:
            for closes, i in evs:
                r = self._trade(strat, closes, i)
                if r is None:
                    continue
                st = self.stats.setdefault(strat["id"],
                                           {"trades": 0, "wins": 0, "sum_ret": 0.0, "util": 0.0, "def": strat})
                st["trades"] += 1
                st["wins"] += 1 if r > 0 else 0
                st["sum_ret"] += r
                st["util"] = round((st["util"] * (st["trades"] - 1) + (r if r >= 0 else self.lam * r))
                                   / st["trades"], 4)               # loss-averse expectancy
                ran += 1
        return {"ran": ran, "strategies": len(self.book)}

    def leaderboard(self, k: int = 8) -> list[dict]:
        rows = [{"id": sid, "style": s["def"]["style"], "n": s["trades"],
                 "win": round(s["wins"] / s["trades"], 3), "util": s["util"],
                 "rule": f"{s['def']['stop']}/{s['def']['target']}/{s['def']['hold']}d move>{s['def']['min_move']}"}
                for sid, s in self.stats.items() if s["trades"] >= self.min_trades]
        rows.sort(key=lambda r: -r["util"])
        return rows[:k]

    def champion(self):
        lb = self.leaderboard(1)
        return self.stats[lb[0]["id"]]["def"] if lb else None

    def adopt_blend(self, cfg: dict, k: int = 5):
        """MIXTURE of strategies: take the MEDIAN rule of the top-K (robust — not one lucky champion,
        which is the anti-overfit move) and make THAT the live strategy. This is 'mixtures of strategies'."""
        lb = self.leaderboard(k)
        if not lb:
            return None
        defs = [self.stats[r["id"]]["def"] for r in lb]
        blend = {"stop": round(statistics.median([d["stop"] for d in defs])),
                 "target": round(statistics.median([d["target"] for d in defs])),
                 "hold": round(statistics.median([d["hold"] for d in defs]))}
        for cat in cfg["catalysts"]:
            self.core.know["catalyst_rules"][cat] = {"stop_pct": blend["stop"], "target_pct": blend["target"],
                                                     "hold_days": blend["hold"]}
        self.core.know["champion_strategy"] = {"blend_of_top": len(defs),
                                               "styles": sorted({d["style"] for d in defs}), **blend}
        return blend

    def _mutate(self, s: dict) -> dict:
        def jit(v, opts):
            return random.choice([x for x in opts if x != v] or [v])
        return {"style": random.choice(STYLES) if random.random() < 0.2 else s["style"],
                "min_move": jit(s["min_move"], (3, 5, 8)), "stop": jit(s["stop"], (5, 6, 8, 10, 12)),
                "target": jit(s["target"], (8, 10, 12, 15, 20, 25)), "hold": jit(s["hold"], (2, 3, 5, 7, 10)),
                "uptrend": s["uptrend"] if random.random() < 0.8 else (not s["uptrend"])}

    def evolve_book(self) -> dict:
        """Strategies CHANGE: retire the worst, breed mutations of the best — survival of the fittest, so
        the population keeps drifting toward what actually beats the market."""
        ranked = self.leaderboard(len(self.book))
        if len(ranked) < max(8, len(self.book) // 2):
            return {"evolved": 0}
        cut = max(3, len(ranked) // 5)
        top = ranked[:cut]
        bottom_ids = [r["id"] for r in ranked[-cut:]]
        evolved = 0
        for sid in bottom_ids:
            parent = self.stats[random.choice(top)["id"]]["def"]
            child = {"id": sid, **self._mutate(parent)}
            for i, b in enumerate(self.book):
                if b["id"] == sid:
                    self.book[i] = child
                    break
            self.stats[sid] = {"trades": 0, "wins": 0, "sum_ret": 0.0, "util": 0.0, "def": child}
            evolved += 1
        return {"evolved": evolved}
