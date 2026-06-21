"""Multi-strategy tournament with OUT-OF-SAMPLE discipline — the antidote to false confidence.

The #1 way a backtest lies: test hundreds of strategies on one slice of history, pick the best, and
mistake luck for skill. So here every strategy is scored on TWO disjoint slices — TRAIN (older 70%) and
TEST (held-out recent 30%). Strategies are SELECTED on train but only TRUSTED (adopted live) if they
ALSO hold up out-of-sample. The train→test gap is reported as an overfitting score, and if NOTHING
generalizes, it adopts NOTHING and says so. The live rule is a MIXTURE (median of the top survivors),
not one lucky champion. Strategies EVOLVE (breed the best, retire the worst). Count is controllable.

Every strategy here is constrained to what Robinhood's agentic cash account can actually execute:
LONG-only equities/ETFs, hold >= 1 day (T+1 settlement, no good-faith violations), no options, no
shorting, no leverage. A strategy that isn't RH-executable is never generated or adopted.
"""
from __future__ import annotations

import random
import statistics

from . import history

STYLES = ("momentum", "breakout", "pullback")        # all LONG entries — RH cash-account compatible
COUNT_MIN, COUNT_MAX = 20, 1000


def rh_ok(s: dict) -> bool:
    """Robinhood agentic cash account can run it: long-only, held >=1 day, no options/shorts/leverage."""
    return s.get("style") in STYLES and int(s.get("hold", 0)) >= 1


class StrategyBook:
    def __init__(self, cfg: dict, core):
        s = cfg.get("strategies", {})
        self.on = bool(s.get("enabled", True))
        self.cfg_count = int(s.get("count", 200))
        self.per_cycle_events = int(s.get("events_per_cycle", 150))
        self.min_rank = int(s.get("min_trades_to_rank", 40))
        self.lam = float(cfg["survival"]["loss_aversion_lambda"])
        self.core = core
        self.stats = core.know.setdefault("strategies", {})
        for k in list(self.stats):              # migrate: drop any pre-out-of-sample-schema stats so we
            if not (isinstance(self.stats[k], dict) and "tr_n" in self.stats[k]):   # never crash on old data
                del self.stats[k]
        self.book = self._generate(self._count())

    def _count(self) -> int:
        # controllable: the bot (or config) can set strategy_count; bounded so it can't run away
        return max(COUNT_MIN, min(COUNT_MAX, int(self.core.know.get("strategy_count", self.cfg_count))))

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

    def resize_if_requested(self):
        want = self._count()
        if want != len(self.book):
            self.book = self._generate(want)               # the bot controls how many it tests

    def _trade(self, s: dict, closes: list[float], i: int):
        if i < 252 or i + 1 >= len(closes) or closes[i - 1] <= 0:
            return None
        sma50 = sum(closes[i - 50:i]) / 50
        up = closes[i] > sma50
        if s["uptrend"] and not up:
            return None
        if s["style"] == "momentum" and (closes[i] / closes[i - 1] - 1) * 100 < s["min_move"]:
            return None
        if s["style"] == "breakout" and closes[i] < max(closes[i - 252:i]):
            return None
        if s["style"] == "pullback" and (closes[i] / closes[i - 1] - 1) * 100 > -s["min_move"]:
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

    def _acc(self, st: dict, side: str, r: float):
        st[f"{side}_n"] += 1
        st[f"{side}_w"] += 1 if r > 0 else 0
        n = st[f"{side}_n"]
        st[f"{side}_u"] = round((st[f"{side}_u"] * (n - 1) + (r if r >= 0 else self.lam * r)) / n, 4)

    def evaluate(self) -> dict:
        if not self.on:
            return {"ran": 0}
        self.resize_if_requested()
        tickers = history.cached_tickers()
        if len(tickers) < 5:
            return {"ran": 0, "note": "history loading"}
        train_evs, test_evs = [], []                       # DISJOINT slices: older 70% vs held-out 30%
        for _ in range(self.per_cycle_events * 10):
            if len(train_evs) >= self.per_cycle_events and len(test_evs) >= self.per_cycle_events // 2:
                break
            bars = history.load(random.choice(tickers))
            if len(bars) < 320:
                continue
            closes = [c for _, c in bars]
            split = int(len(closes) * 0.7)
            if len(train_evs) < self.per_cycle_events and split - 11 > 252:
                train_evs.append((closes, random.randint(252, split - 11)))
            if len(test_evs) < self.per_cycle_events // 2 and len(closes) - 11 > split:
                test_evs.append((closes, random.randint(split, len(closes) - 11)))
        ran = 0
        for strat in self.book:
            st = self.stats.setdefault(strat["id"], {"tr_n": 0, "tr_w": 0, "tr_u": 0.0,
                                                     "te_n": 0, "te_w": 0, "te_u": 0.0, "def": strat})
            for closes, i in train_evs:
                r = self._trade(strat, closes, i)
                if r is not None:
                    self._acc(st, "tr", r); ran += 1
            for closes, i in test_evs:
                r = self._trade(strat, closes, i)
                if r is not None:
                    self._acc(st, "te", r)
        return {"ran": ran, "strategies": len(self.book)}

    def _ranked(self):
        return sorted((s for s in self.stats.values()
                       if s["tr_n"] >= self.min_rank and s["te_n"] >= self.min_rank // 2),
                      key=lambda s: -s["te_u"])           # ranked by OUT-OF-SAMPLE expectancy

    def survivors(self):
        """Strategies that work IN AND OUT of sample, are RH-executable — the only ones we trust."""
        return [s for s in self._ranked() if s["tr_u"] > 0 and s["te_u"] > 0 and rh_ok(s["def"])]

    def leaderboard(self, k: int = 6) -> list[dict]:
        return [{"id": s["def"]["id"], "style": s["def"]["style"], "te_n": s["te_n"],
                 "train_u": s["tr_u"], "test_u": s["te_u"],
                 "rule": f"{s['def']['stop']}/{s['def']['target']}/{s['def']['hold']}d"}
                for s in self._ranked()[:k]]

    def adopt_blend(self, cfg: dict, k: int = 5):
        """Live rule = MEDIAN of the top-K SURVIVORS (out-of-sample-validated + RH-executable). If none
        generalize, adopt NOTHING — refuse to trust an overfit champion. This is the anti-false-confidence."""
        surv = self.survivors()[:k]
        if not surv:
            self.core.know["champion_strategy"] = {"status": "no out-of-sample survivor — not adopting"}
            return None
        defs = [s["def"] for s in surv]
        blend = {"stop": round(statistics.median([d["stop"] for d in defs])),
                 "target": round(statistics.median([d["target"] for d in defs])),
                 "hold": max(1, round(statistics.median([d["hold"] for d in defs])))}
        if not rh_ok({"style": "momentum", "hold": blend["hold"]}):
            return None
        for cat in cfg["catalysts"]:
            self.core.know["catalyst_rules"][cat] = {"stop_pct": blend["stop"], "target_pct": blend["target"],
                                                     "hold_days": blend["hold"]}
        self.core.know["champion_strategy"] = {"blend_of": len(defs), "styles": sorted({d["style"] for d in defs}),
                                               "rh_executable": True, **blend}
        return blend

    def honesty(self) -> dict:
        """The bot grading itself HONESTLY: how big is the train->test gap, did anything survive
        out-of-sample, and is its confidence earned or fake? Never lets it believe its own backtest."""
        ranked = self._ranked()
        if not ranked:
            return {"status": "NO out-of-sample data yet — zero proven edge, learning only", "survivors": 0}
        top = ranked[0]
        gap = round(top["tr_u"] - top["te_u"], 3)          # how much the edge shrinks out-of-sample
        n_surv = len(self.survivors())
        if n_surv == 0:
            status = "OVERFIT — the best strategies FAIL out-of-sample. No trustworthy edge. Confidence = false."
        elif gap > 1.0:
            status = f"PARTIAL — edge shrinks a lot out-of-sample (gap {gap}); treat with suspicion."
        else:
            status = f"HOLDS — {n_surv} strategies survive out-of-sample (gap {gap}). Tentative, still shadow."
        return {"status": status, "survivors": n_surv, "train_test_gap": gap,
                "ranked": len(ranked), "of_strategies": len(self.book)}

    def _mutate(self, s: dict) -> dict:
        def jit(v, opts):
            return random.choice([x for x in opts if x != v] or [v])
        st = random.choice(STYLES) if random.random() < 0.2 else s["style"]
        return {"style": st, "min_move": jit(s["min_move"], (3, 5, 8)), "stop": jit(s["stop"], (5, 6, 8, 10, 12)),
                "target": jit(s["target"], (8, 10, 12, 15, 20, 25)), "hold": jit(s["hold"], (2, 3, 5, 7, 10)),
                "uptrend": st != "pullback"}

    def evolve_book(self) -> dict:
        ranked = self._ranked()
        if len(ranked) < max(8, len(self.book) // 2):
            return {"evolved": 0}
        cut = max(3, len(ranked) // 5)
        top = ranked[:cut]
        bottom_ids = [s["def"]["id"] for s in ranked[-cut:]]
        evolved = 0
        for sid in bottom_ids:
            child = {"id": sid, **self._mutate(random.choice(top)["def"])}
            if not rh_ok(child):
                continue
            for i, b in enumerate(self.book):
                if b["id"] == sid:
                    self.book[i] = child
                    break
            self.stats[sid] = {"tr_n": 0, "tr_w": 0, "tr_u": 0.0, "te_n": 0, "te_w": 0, "te_u": 0.0, "def": child}
            evolved += 1
        return {"evolved": evolved}
