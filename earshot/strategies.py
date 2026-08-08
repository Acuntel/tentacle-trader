"""Multi-strategy tournament with WALK-FORWARD discipline — the antidote to false confidence.

The #1 way a backtest lies: test hundreds of strategies on one slice of history, pick the best, and
mistake luck for skill. A single 70/30 split barely helps — the 'test' tail can be lucky too. So here
every event is bucketed into one of K time-ordered FOLDS (walk-forward cross-validation), and a
strategy is only TRUSTED (adopted live) if it stays positive-expectancy across a MAJORITY of folds AND
across market regimes (uptrend + downtrend), not just on one stretch. The fold spread is reported as an
overfit score; if NOTHING generalizes, it adopts NOTHING and says so. The live rule is a MIXTURE
(median of the survivors), not one lucky champion. Strategies EVOLVE (breed the best, retire the worst).

Every strategy is constrained to what Robinhood's agentic cash account can execute: LONG-only
equities/ETFs, hold >= 1 day (T+1 settlement), no options, no shorting, no leverage.
"""
from __future__ import annotations

import math
import random
import statistics

from . import history, marketadj

STYLES = ("momentum", "breakout", "pullback")        # all LONG entries — RH cash-account compatible
COUNT_MIN, COUNT_MAX = 20, 1000


def rh_ok(s: dict) -> bool:
    """Robinhood agentic cash account can run it: long-only, held >=1 day, no options/shorts/leverage."""
    return s.get("style") in STYLES and int(s.get("hold", 0)) >= 1


class StrategyBook:
    def __init__(self, cfg: dict, core):
        s = cfg.get("strategies", {})
        v = cfg.get("validation", {})
        self.on = bool(s.get("enabled", True))
        self.cfg_count = int(s.get("count", 200))
        self.per_cycle_events = int(s.get("events_per_cycle", 150))
        self.min_rank = int(s.get("min_trades_to_rank", 40))
        self.lam = float(cfg["survival"]["loss_aversion_lambda"])
        self.folds = max(2, int(v.get("walk_forward_folds", 4)))
        self.min_fold_winrate = float(v.get("min_fold_winrate", 0.5))
        self.regime_stratify = bool(v.get("regime_stratify", True))
        self.core = core
        self.stats = core.know.setdefault("strategies", {})
        for k in list(self.stats):              # migrate: drop any pre-walk-forward stats so we never
            if not (isinstance(self.stats[k], dict) and "folds" in self.stats[k]):  # crash on old schema
                del self.stats[k]
            elif not self.stats[k].get("cal"):  # AUDIT: fold semantics changed to CALENDAR bins —
                del self.stats[k]               # relative-fold stats must not mix into calendar folds
        self.book = self._generate(self._count())
        self._inject_hive()                     # designed strategies from the Hive strategist compete too

    def _count(self) -> int:
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
        # AUDIT FIX: seeded shuffle — an UNSEEDED shuffle re-mapped every id to a different parameter
        # set on each boot, so persisted stats kept accruing under ids whose definition had changed
        # (chimeric records, e.g. S0000 n=135 across several different strategies).
        random.Random(7).shuffle(grid)
        return [{"id": f"S{i:04d}", **grid[i % len(grid)]} for i in range(n)]

    @staticmethod
    def _params(d: dict) -> dict:
        return {k: v for k, v in (d or {}).items() if k != "id"}

    def _inject_hive(self):
        """AUDIT FIX: the Hive STRATEGIST's designs used to land in know['hive']['strategies'] and go
        NOWHERE (the only consumers were the coordinator prompt and a count) — an entire agent role
        producing dead output. Designs that carry the executable grid parameters now enter the book
        as H-* entries and COMPETE in the same walk-forward tournament as everything else."""
        seen_ids = {s["id"] for s in self.book}
        for d in (self.core.know.get("hive", {}).get("strategies") or [])[-20:]:
            try:
                s = {"style": str(d.get("style", "")), "min_move": int(d.get("min_move", 0)),
                     "stop": int(d.get("stop", 0)), "target": int(d.get("target", 0)),
                     "hold": int(d.get("hold", d.get("hold_days", 0))),
                     "uptrend": bool(d.get("uptrend", True))}
            except Exception:
                continue
            if not rh_ok(s) or not (1 <= s["min_move"] <= 15 and 3 <= s["stop"] <= 15
                                    and 5 <= s["target"] <= 30 and 1 <= s["hold"] <= 15):
                continue
            sid = f"H-{s['style'][:4]}-{s['min_move']}-{s['stop']}-{s['target']}-{s['hold']}"
            if sid not in seen_ids:
                self.book.append({"id": sid, **s})
                seen_ids.add(sid)

    def resize_if_requested(self):
        want = self._count()
        if want != len(self.book) - sum(1 for s in self.book if s["id"].startswith("H-")):
            self.book = self._generate(want)               # the bot controls how many it tests
            self._inject_hive()

    def _trade(self, s: dict, closes: list[float], dates: list[str], i: int):
        """Return the ALPHA of the trade (return minus SPY over the same dates) so the tournament
        selects strategies with real edge, not strategies that just ride a rising market."""
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
        ret, j_exit = 0.0, min(i + s["hold"], len(closes) - 1)
        for j in range(i + 1, min(i + 1 + s["hold"], len(closes))):
            chg = (closes[j] / entry - 1) * 100
            if chg <= -s["stop"]:
                ret, j_exit = -float(s["stop"]), j; break
            if chg >= s["target"]:
                ret, j_exit = float(s["target"]), j; break
            ret, j_exit = chg, j
        mkt = marketadj.market_return(dates[i], dates[min(j_exit, len(dates) - 1)])
        return round(ret - mkt, 3) if mkt is not None else round(ret, 3)

    def _bump(self, b: dict, r: float):
        b["n"] += 1
        b["w"] += 1 if r > 0 else 0
        n = b["n"]
        b["u"] = round((b["u"] * (n - 1) + (r if r >= 0 else self.lam * r)) / n, 4)

    @staticmethod
    def _blank(strat: dict) -> dict:
        return {"folds": {}, "reg": {}, "n": 0, "def": strat, "cal": True}

    def evaluate(self) -> dict:
        if not self.on:
            return {"ran": 0}
        self.resize_if_requested()
        tickers = history.cached_tickers()
        if len(tickers) < 5:
            return {"ran": 0, "note": "history loading"}
        embargo = 12                                       # > the longest hold -> no train/test leakage
        events = []                                        # (closes, dates, i, fold, regime)
        tickers_local = list(tickers)
        random.shuffle(tickers_local)
        for t in tickers_local:
            if len(events) >= self.per_cycle_events:
                break
            bars = history.load(t)
            if len(bars) < 320:
                continue
            dates = [d for d, _ in bars]
            closes = [c for _, c in bars]
            L = len(closes)
            i = random.randint(252, L - 11)
            # AUDIT FIX: folds are CALENDAR bins over a fixed 10y window, not per-ticker relative
            # position — histories of different lengths (2016-2026 vs 2024-2026) used to land the SAME
            # calendar period in different folds, quietly breaking the walk-forward time separation.
            import datetime as _dt
            try:
                d_ord = _dt.date.fromisoformat(dates[i]).toordinal()
            except Exception:
                continue
            today_ord = _dt.date.today().toordinal()
            span = 3650.0
            rel = 1.0 - (today_ord - d_ord) / span
            if rel < 0:
                continue                                    # older than the window
            # PURGE + EMBARGO: drop events whose hold window could straddle a fold boundary, so a
            # train trade never leaks into the adjacent test fold (leak-free walk-forward CV).
            boundaries = [today_ord - span + k * span / self.folds for k in range(1, self.folds)]
            if boundaries and min(abs(d_ord - b) for b in boundaries) < max(embargo, 18):
                continue
            fold = min(self.folds - 1, int(min(0.999, rel) * self.folds))   # calendar walk-forward bin
            regime = "up" if closes[i] > (sum(closes[i - 50:i]) / 50) else "down"
            events.append((closes, dates, i, fold, regime))
        ran = 0
        for strat in self.book:
            st = self.stats.setdefault(strat["id"], self._blank(strat))
            # AUDIT FIX (belt + suspenders with the seeded shuffle): stats must never keep accruing
            # under an id whose DEFINITION changed (boot remap, grid change, count change) — reset.
            if self._params(st.get("def")) != self._params(strat):
                st = self.stats[strat["id"]] = self._blank(strat)
            for closes, dates, i, fold, regime in events:
                r = self._trade(strat, closes, dates, i)
                if r is None:
                    continue
                self._bump(st["folds"].setdefault(str(fold), {"n": 0, "w": 0, "u": 0.0}), r)
                self._bump(st["reg"].setdefault(regime, {"n": 0, "w": 0, "u": 0.0}), r)
                st["n"] += 1
                ran += 1
        return {"ran": ran, "strategies": len(self.book)}

    def _fold_us(self, st: dict) -> list[float]:
        floor = max(8, self.min_rank // self.folds)
        return [b["u"] for b in st.get("folds", {}).values() if b["n"] >= floor]

    def _score(self, st: dict) -> float:
        us = self._fold_us(st)
        return statistics.mean(us) if us else -99.0

    def _luck_threshold(self) -> float:
        """Deflated-Sharpe-style multiple-testing floor. With N strategies tried, the BEST one's
        expectancy under NO real edge is ~ sigma * sqrt(2*ln N). A 'survivor' must beat THAT, so we
        never crown a strategy that only looks good because we tried hundreds of variants."""
        scores = [float(self._score(s)) for s in self._ranked()]
        if len(scores) < 5:
            return 0.0
        # hand-rolled population stdev: statistics.pstdev crashes on mixed numeric types from
        # JSON round-trips ('float' object has no attribute 'numerator', py3.11) — and this line
        # dying mid-coach silently killed the daily Telegram digest for a week (Aug 1-7)
        mu = sum(scores) / len(scores)
        sigma = math.sqrt(sum((x - mu) ** 2 for x in scores) / len(scores))
        return sigma * math.sqrt(2 * math.log(max(2, len(scores))))

    def _ranked(self):
        elig = [s for s in self.stats.values()
                if s.get("n", 0) >= self.min_rank and len(self._fold_us(s)) >= max(2, self.folds // 2)]
        return sorted(elig, key=self._score, reverse=True)    # by mean OUT-OF-SAMPLE fold expectancy

    def survivors(self):
        """Strategies positive across a MAJORITY of walk-forward folds AND every regime with enough
        data, and RH-executable — the only ones we trust live. This is the anti-false-confidence gate."""
        need = math.ceil(self.folds * self.min_fold_winrate)
        floor = self._luck_threshold()                      # multiple-testing-adjusted edge bar
        out = []
        for s in self._ranked():
            us = self._fold_us(s)
            if sum(1 for u in us if u > 0) < need:
                continue
            if self._score(s) <= floor:                     # not beyond best-of-N luck -> reject
                continue
            if self.regime_stratify:
                reg = s.get("reg", {})
                both = [g for g in ("up", "down") if reg.get(g, {}).get("n", 0) >= self.min_rank // 2]
                if both and not all(reg[g]["u"] > 0 for g in both):
                    continue
            if rh_ok(s["def"]):
                out.append(s)
        return out

    def leaderboard(self, k: int = 6) -> list[dict]:
        out = []
        for s in self._ranked()[:k]:
            us = self._fold_us(s)
            out.append({"id": s["def"]["id"], "style": s["def"]["style"], "n": s["n"],
                        "folds_pos": sum(1 for u in us if u > 0), "folds": len(us),
                        "oos_u": round(statistics.mean(us), 3) if us else 0.0,
                        "rule": f"{s['def']['stop']}/{s['def']['target']}/{s['def']['hold']}d"})
        return out

    def adopt_blend(self, cfg: dict, k: int = 5):
        """Live rule = MEDIAN of the top-K SURVIVORS (walk-forward + regime validated + RH-executable).
        If none generalize, adopt NOTHING — refuse to trust an overfit champion."""
        surv = self.survivors()[:k]
        if not surv:
            self.core.know["champion_strategy"] = {"status": "no walk-forward survivor — not adopting"}
            return None
        defs = [s["def"] for s in surv]
        blend = {"stop": round(statistics.median([d["stop"] for d in defs])),
                 "target": round(statistics.median([d["target"] for d in defs])),
                 "hold": max(1, round(statistics.median([d["hold"] for d in defs])))}
        if not rh_ok({"style": "momentum", "hold": blend["hold"]}):
            self.core.know["champion_strategy"] = {"status": "blend rejected — not RH-executable"}
            return None
        for cat in cfg["catalysts"]:
            self.core.know["catalyst_rules"][cat] = {"stop_pct": blend["stop"], "target_pct": blend["target"],
                                                     "hold_days": blend["hold"]}
        self.core.know["champion_strategy"] = {"blend_of": len(defs), "styles": sorted({d["style"] for d in defs}),
                                               "rh_executable": True, "validated": "walk_forward", **blend}
        return blend

    def honesty(self) -> dict:
        """The bot grading itself HONESTLY: how wide is the fold spread, did anything survive
        walk-forward, is its confidence earned or fake? Never lets it believe its own backtest."""
        ranked = self._ranked()
        if not ranked:
            return {"status": "NO out-of-sample data yet — zero proven edge, learning only", "survivors": 0}
        us = self._fold_us(ranked[0])
        folds_pos = sum(1 for u in us if u > 0)
        spread = round(max(us) - min(us), 3) if us else 0.0
        floor = round(self._luck_threshold(), 3)            # multiple-testing bar
        top = round(self._score(ranked[0]), 3)
        n_surv = len(self.survivors())
        if n_surv == 0:
            status = (f"OVERFIT — best strategy's edge {top} ≤ the multiple-testing luck floor {floor} "
                      f"(tried {len(ranked)}). No trustworthy edge. Confidence = false.")
        elif spread > 1.5:
            status = f"PARTIAL — edge swings across folds (spread {spread}); treat with suspicion."
        else:
            status = f"HOLDS — {n_surv} strategies beat the luck floor {floor} across {folds_pos}/{len(us)} folds. Tentative, still shadow."
        return {"status": status, "survivors": n_surv, "fold_spread": spread, "folds_positive": folds_pos,
                "luck_floor": floor, "top_edge": top, "folds": len(us), "ranked": len(ranked),
                "of_strategies": len(self.book)}

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
            self.stats[sid] = self._blank(child)
            evolved += 1
        return {"evolved": evolved}
