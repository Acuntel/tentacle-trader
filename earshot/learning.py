"""Constant learning: log every candidate, resolve outcomes, re-weight sources, and —
after every loss — go into the Dungeon and write down what went wrong."""
from __future__ import annotations

import json
import os
import time

from .models import Candidate, Outcome, iso
from .patterns import PatternMemory, field_rate, update_field_rate
from . import costs, market, prices

OPEN_PATH = "state/open_positions.json"
OBS_PATH = "state/observations.json"
OUTCOMES_PATH = "state/outcomes.jsonl"
KNOW_PATH = "state/knowledge.json"
DUNGEON_PATH = "state/dungeon/post_mortems.jsonl"  # under state/ so one volume persists everything
RESOLVED_OBS_PATH = "state/resolved_obs.jsonl"     # ledger of labeled setups -> warm-starts the P(win) model
LEARN_OPEN_PATH = "state/learning_positions.json"  # WS1: off-book exploration trades (never touch mastery/$)
LEARN_OUT_PATH = "state/learning_outcomes.jsonl"   # WS1: ledger of resolved learning trades
WMIN, WMAX = 0.1, 1.2
OBS_GRACE_H = 144.0   # hours past the LONGEST horizon before an unresolvable obs is dropped for good


from .persist import load_json as _load, save_json as _save   # atomic + .bak — never lose progress


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Learner:
    def __init__(self, weights: dict, priors: dict | None = None):
        self.weights = weights  # SHARED dict — updates here propagate to live tentacles
        self.priors = dict(priors or {})  # config source_weights = the anchor learned weights orbit
        self.open = _load(OPEN_PATH, [])
        self.learn_open = _load(LEARN_OPEN_PATH, [])  # WS1: off-book learning trades (no mastery/equity)
        self.obs = _load(OBS_PATH, [])      # pending observations (learn-from-all-data)
        self.know = _load(KNOW_PATH, {"sources": {}, "catalysts": {}, "weights": {}})
        # restore LEARNED weights from the persistent ledger so progress survives redeploys
        if self.know.get("weights"):
            self.weights.update(self.know["weights"])
        self.patterns = PatternMemory(self.know)   # learned pattern win-rates (persisted)
        self.lgate = None                          # LearnedGate (P(win) model), attached by main
        self.core = None                           # shared BrainCore, attached by main
        # progressive resolution horizons: (hours_since_entry, credit_rate). Short = noisier = lighter.
        # The long horizon is the real label; the short ones let learning accrue in HOURS, not days.
        self._horizons = [(6.0, 0.10), (24.0, 0.05)]            # default; main overrides from config.learning

    def _persist_know(self):
        self.know["weights"] = self.weights      # learned weights ride along, survive restarts
        _save(KNOW_PATH, self.know)

    def _head(self, key: str):
        """W2.1/W2.3: shared model heads (per-horizon + directional) over the SAME knowledge dict the
        primary lgate persists to — see learnedgate.head() for why instances must be shared."""
        from .learnedgate import head
        return head(self.know, key)

    def open_position(self, c: Candidate, fill_price: float):
        self.open.append({
            "ticker": c.ticker, "catalyst": c.catalyst.value, "confidence": c.confidence,
            "size_usd": c.size_usd, "entry_price": fill_price, "entry_ts": time.time(),
            "hold_days": c.hold_days, "target_pct": c.target_pct, "stop_pct": c.stop_pct,
            "runup_pct": c.runup_pct, "sources": sorted({s.source for s in c.signals}),
            "pattern": getattr(c, "_signature", ""),
            "lgate_features": getattr(c, "_lgate_features", None),   # train the P(win) model on exit
            "features": getattr(c, "_features", None),               # persisted feature history (fundamentals/quality)
            "pwin": getattr(c, "_pwin", None),
        })
        _save(OPEN_PATH, self.open)

    def resolve_due(self, instinct, exits, bearish_tickers=None, extended: bool = False) -> list[dict]:
        """Manage every open position against the LIVE price each cycle via the exit engine.
        Positions now close early on stop/target/trailing/decay, not just at the time horizon.
        extended=True (pre/post sessions): only FRESH extended prints count — a name with no
        off-hours print simply can't exit until the open (honest, same rule as entries)."""
        still_open, resolved = [], []
        for p in self.open:
            price = prices.last_price(p["ticker"], extended=extended)
            if price and p.get("entry_price"):
                reason, chg = exits.evaluate(p, price, bearish_tickers)
            else:  # no live price -> fall back to horizon resolution
                ret = prices.forward_return_pct(p["ticker"], p["entry_ts"], p["hold_days"])
                reason, chg = (("time", ret) if ret is not None else (None, 0.0))
            if not reason:
                still_open.append(p)
                continue
            ret = max(-p["stop_pct"], min(p["target_pct"], chg))   # bound to the disciplined range
            # FRICTION (costs.py): debit the liquidity-tiered round trip BEFORE anything learns from
            # this trade — mastery, patterns, weights and P(win) must all be earned NET of costs.
            rt = costs.round_trip_pct(p["ticker"])
            if market.was_closed(p.get("entry_ts", 0)):
                rt = round(rt * 2.5, 3)        # extended-hours entry: real off-hours spreads run
                                               # several times wider — the debit must match them
            ret = round(ret - rt, 3)
            # ALPHA: the trade's return MINUS the market over the same window. Skill is learned on alpha
            # (so a rising tape isn't mistaken for edge); the $ P&L / equity stay on the raw return.
            mkt = prices.market_return_since(p["entry_ts"])
            alpha = round(ret - mkt, 3) if mkt is not None else ret
            win_a = alpha > 0
            entry = p["entry_price"] or 0.0
            outcome = Outcome(
                ticker=p["ticker"], catalyst=p["catalyst"], confidence=p["confidence"],
                size_usd=p["size_usd"], entry_price=entry,
                exit_price=round(entry * (1 + ret / 100), 4), pnl_pct=ret,
                pnl_usd=round(p["size_usd"] * ret / 100, 2), win=ret > 0,
                entry_ts=p["entry_ts"], exit_ts=time.time(), sources=p["sources"],
            )
            _append(OUTCOMES_PATH, {**outcome.to_dict(), "exit": reason, "alpha": alpha, "cost_pct": rt})
            mind = instinct.record(p["confidence"], ret, p["size_usd"], alpha=alpha)  # mastery on alpha
            self._learn(p, outcome, alpha)
            self.patterns.record(p.get("pattern", ""), win_a, alpha,
                                 usd=outcome.pnl_usd)      # learn the ALPHA pattern; $ vote in grad
            if self.lgate is not None and p.get("lgate_features"):
                self.lgate.update(p["lgate_features"], win_a)              # train P(win) on beat-the-market
            if self.core is not None and p.get("features"):
                self.core.record_features(p["features"], win_a)           # persist the feature history
            if not outcome.win:
                self._dungeon(p, outcome, mind)
            resolved.append({"outcome": outcome.to_dict(), "mind": mind, "exit": reason})
        self.open = still_open
        _save(OPEN_PATH, self.open)
        self._persist_know()
        return resolved

    # ---- WS1: OFF-BOOK learning trades — flood the learners with forward samples for free ----
    def open_learning(self, c: Candidate, fill_price: float, direction: int, notional: float) -> bool:
        """Open an off-book paper 'learning trade' (long or short) purely to harvest a labeled forward
        outcome. Feeds patterns / P(win) / source-weights but NEVER touches real equity, the mastery
        ladder, or the money gate. One per ticker at a time. In shadow, information is free."""
        if any(p["ticker"] == c.ticker for p in self.learn_open):
            return False
        # W2.5: no REOPEN on the same ticker the same day. A persistent signal used to churn
        # stop -> reopen -> stop (live: SABSW resolved 5x on 2026-07-07) — correlated duplicate
        # labels of one event, over-representing volatile fast-exit names in the sample mix.
        today = iso(time.time())[:10]
        if self.know.get("learn_seen", {}).get(c.ticker) == today:
            return False
        self.learn_open.append({
            "ticker": c.ticker, "catalyst": c.catalyst.value, "dir": int(direction),
            "entry_price": fill_price, "entry_ts": time.time(), "notional": notional,
            "hold_days": c.hold_days, "target_pct": c.target_pct, "stop_pct": c.stop_pct,
            "sources": sorted({s.source for s in c.signals}),
            "pattern": getattr(c, "_signature", ""),
            "lgate_features": getattr(c, "_lgate_features", None),
            "pwin": getattr(c, "_pwin", None),
        })
        return True

    def resolve_learning(self, max_open: int = 300, credit_rate: float = 0.10) -> dict:
        """Resolve off-book learning trades on stop / target / time, directionally. The LEARNERS are
        trained on the LONG-perspective label (would a long have beaten the market — independent of the
        side we took), so the long-oriented P(win) model stays consistent; separately we track whether
        the model's DIRECTIONAL call beat the market. instinct.record() is NEVER called (no mastery)."""
        still = []
        n_res = dir_hits = 0
        for p in self.learn_open:
            px = prices.last_price(p["ticker"])
            if not px or not p.get("entry_price"):
                still.append(p); continue
            ret_raw = (px / p["entry_price"] - 1) * 100
            d = int(p.get("dir", 1))
            dir_ret = d * ret_raw                               # the chosen side's P&L return
            age_days = (time.time() - p["entry_ts"]) / 86400
            if dir_ret <= -p.get("stop_pct", 8):
                reason = "stop"
            elif dir_ret >= p.get("target_pct", 12):
                reason = "target"
            elif age_days >= p.get("hold_days", 5):
                reason = "time"
            else:
                still.append(p); continue
            mkt = prices.market_return_since(p["entry_ts"])
            stock_alpha = round(ret_raw - mkt, 3) if mkt is not None else round(ret_raw, 3)
            long_win = stock_alpha > 0                          # LONG-perspective label (bookkeeping, gross)
            # FRICTION (costs.py): our side's harvestable alpha is NET of the liquidity-tiered round
            # trip — the directional head + the explore ledger must never learn a gross-only edge.
            rt = costs.round_trip_pct(p["ticker"])
            dir_alpha = round(d * stock_alpha - rt, 3)           # did OUR side beat the market AFTER costs?
            # W2.4: learning-trade labels are CENSORED at direction-dependent exit times (a short's
            # stop fires at the long's max favorable excursion), so they no longer train the P(win)
            # model, patterns, or source weights — the observation pipeline already labels the SAME
            # ticker/day setups at fixed horizons (crediting them here double-counted one event with
            # a biased measurement time). Learning trades keep what only THEY can teach:
            # W2.3: the DIRECTIONAL head — did the side WE CHOSE beat the market? dir_alpha was
            # ledgered and counted but trained nothing while direction came from a trust-0 threshold.
            if self.lgate is not None and p.get("lgate_features"):
                fd = dict(p["lgate_features"])
                df = float(d)
                fd["dir_long"] = df; fd["dirx_tech"] = df * fd.get("t_bias", 0.0); fd["dirx_opt"] = df * fd.get("opt_bias", 0.0)                              # signed: +1 long / -1 short
                fd["dirx_tech"] = df * fd.get("t_bias", 0.0)     # side x timing signals — lets the
                fd["dirx_opt"] = df * fd.get("opt_bias", 0.0)    # model learn WHICH side the evidence
                fd["dirx_down"] = df * fd.get("downtrend", 0.0)  # actually supports
                self._head("model_dir").update(fd, dir_alpha > 0)
            ex = self.know.setdefault("explore", {"trades": 0, "dir_hits": 0, "sum_alpha": 0.0})
            ex["trades"] += 1
            ex["dir_hits"] += 1 if dir_alpha > 0 else 0
            ex["sum_alpha"] = round(ex["sum_alpha"] + dir_alpha, 3)
            _append(LEARN_OUT_PATH, {"ts": iso(time.time()), "ticker": p["ticker"], "dir": d,
                                     "exit": reason, "ret": round(dir_ret - rt, 3), "alpha": dir_alpha,
                                     "cost": rt, "long_win": long_win, "sig": p.get("pattern", "")})
            seen = self.know.setdefault("learn_seen", {})       # W2.5: block same-day reopen churn
            seen[p["ticker"]] = iso(time.time())[:10]
            n_res += 1
            dir_hits += 1 if dir_alpha > 0 else 0
        if len(still) > max_open:
            # W2.5: enforce the cap by evicting the NEWEST (just-opened, farthest from maturity) —
            # the old [-max_open:] slice threw away the OLDEST entries, i.e. exactly the pending
            # time-exits about to yield a label, skewing the sample mix toward fast movers.
            still = still[:max_open]
        self.learn_open = still
        _save(LEARN_OPEN_PATH, self.learn_open)
        if n_res:
            today = iso(time.time())[:10]                       # keep the reopen-guard map small
            self.know["learn_seen"] = {t: dy for t, dy in self.know.get("learn_seen", {}).items()
                                       if dy == today}
            self._persist_know()
        return {"resolved": n_res, "dir_hits": dir_hits, "open": len(self.learn_open)}

    def _credit_sources(self, sources, win: bool, ret: float, rate: float = 0.15):
        """Nudge each source's weight toward its proven RELATIVE precision (on ALPHA). Shared by trades
        AND observations (gentler rate for observations).

        Two hard-won anchors:
        • FIELD-RELATIVE, not 0.5-absolute. Alpha-wins have a ~0.34 base rate, so judging sources against
          an implicit 0.5 dragged EVERY firing source to the weight floor while dead sources kept their
          priors (trust inversion). Neutral = winning at the field rate; above the field rises, below sinks.
        • PRIOR-ANCHORED, not self-referential. The old target was a multiple of the CURRENT weight, so a
          wrongly-punished source could never climb back (the floor was absorbing). Anchoring the target on
          the config prior × relative precision makes the weight a stationary function of the evidence —
          it self-heals from any bad state instead of random-walking into a corner.

        Includes DRIFT detection: if a source's recent window has decayed well below its lifetime
        precision, cut it faster — markets are non-stationary, a stale edge should fade quickly."""
        update_field_rate(self.know, win)            # one labeled outcome -> the shared base-rate tracker
        fld = field_rate(self.know)
        for src in sources:
            st = self.know["sources"].setdefault(src, {"trades": 0, "wins": 0, "sum_ret": 0.0})
            st["trades"] += 1
            st["wins"] += 1 if win else 0
            st["sum_ret"] += ret
            rec = st.setdefault("recent", [])
            rec.append(1 if win else 0)
            st["recent"] = rec[-20:]
            precision = st["wins"] / st["trades"]
            base = self.weights.get(src, 0.5)
            prior = self.priors.get(src, 0.5)
            # at-field -> the prior; edge scales it. ASYMMETRIC slopes (losses cut harder than wins
            # lift, matching the mother-instinct loss-aversion) so a truly bad source sinks decisively.
            slope = 1.2 if precision >= fld else 2.5
            target = prior * (1.0 + slope * (precision - fld))
            # WS3 adaptive rate: a source with FEW resolved outcomes earns/loses trust faster (we're
            # ignorant of it), settling to the base rate once it has a real track record (>=40).
            eff_rate = rate if st["trades"] >= 40 else min(0.4, rate * (1.0 + (40 - st["trades"]) / 40.0))
            w = (1 - eff_rate) * base + eff_rate * target
            if len(st["recent"]) >= 10:              # DRIFT: recent edge collapsed (regime shift) -> cut fast
                recent_p = sum(st["recent"]) / len(st["recent"])
                if recent_p < precision - 0.2:
                    w *= 0.85
            self.weights[src] = round(max(WMIN, min(WMAX, w)), 3)

    def _learn(self, p: dict, o: Outcome, alpha: float | None = None):
        a = o.pnl_pct if alpha is None else alpha    # learn on ALPHA (market-adjusted), not raw return
        self._credit_sources(p["sources"], a > 0, a, rate=0.15)
        ct = self.know["catalysts"].setdefault(o.catalyst, {"trades": 0, "wins": 0, "sum_ret": 0.0})
        ct["trades"] += 1
        ct["wins"] += 1 if a > 0 else 0
        ct["sum_ret"] += a

    # ---- LEARN FROM ALL DATA (not just trades): label every setup we fully worked up ----
    def observe(self, candidates, max_pending: int = 800):
        """Record every setup we SEE and fully analyzed (traded or not) so it becomes a labeled example.
        One per ticker per day. Widened from multi-family-only to everything in LEARN SCOPE (incl. the
        bounded near-miss workups): the resolved-obs ledger is what feeds the P(win) model, the Forge,
        semantic memory, and warm-start — every extra observed setup compounds through all of them."""
        from . import prices
        today = iso(time.time())[:10]
        have = {(o["ticker"], o["day"]) for o in self.obs}
        for c in candidates:
            if not (getattr(c, "_qualified_learn", False) or getattr(c, "_families", 1) >= 2):
                continue
            if (c.ticker, today) in have:
                continue
            # AUDIT FIX: lead-time recorded AFTER the per-ticker/day dedup — it used to fire on every
            # 10s tick for a persistent candidate, double-counting the same setup hundreds of times.
            if self.core is not None:
                self.core.record_leadtime(c)          # which source got here FIRST (lead-time learning)
            px = prices.last_price(c.ticker)
            if not px:
                continue
            # AT-CAP EVICTION (W1.1): the buffer used to REJECT the incoming setup when full, so a
            # clog of stale obs silently starved learning of every NEW setup. Evict the oldest obs
            # that already yielded a label (its short horizons resolved — the cheapest to lose),
            # else the oldest outright; obs are appended in ts order, so index 0 is oldest.
            while len(self.obs) >= max_pending:
                idx = next((i for i, o in enumerate(self.obs) if o.get("done")), 0)
                self.obs.pop(idx)
            self.obs.append({"ticker": c.ticker, "day": today, "ts": time.time(),
                             "entry": px, "sig": getattr(c, "_signature", ""),
                             "sources": sorted({s.source for s in c.signals}),
                             "feat": getattr(c, "_lgate_features", None)})
            have.add((c.ticker, today))
        _save(OBS_PATH, self.obs)

    def set_horizons(self, hours: list, rates: list):
        """Wire the progressive-resolution horizons from config (parallel lists)."""
        pairs = [(float(h), float(r)) for h, r in zip(hours, rates) if float(h) > 0]
        if pairs:
            self._horizons = sorted(pairs)

    def _ledger_resolved(self, o: dict, h: float, ret: float, win: bool, age_h: float | None = None):
        """Append every labeled observation to an immutable ledger so a fresh boot can WARM-START the
        P(win) model from real feature vectors instead of starting cold."""
        if not o.get("feat"):
            return
        row = {"ts": iso(time.time()), "h": h, "ret": round(ret, 3),
               "win": win, "feat": o["feat"], "sig": o.get("sig", "")}
        if age_h is not None:
            row["age_h"] = round(age_h, 1)     # W2.1: the ACTUAL age at labeling, not the nominal horizon
        _append(RESOLVED_OBS_PATH, row)

    def resolve_observations(self, max_per_cycle: int = 40) -> int:
        """PROGRESSIVE resolution: label each observation at MULTIPLE horizons (e.g. 1h / 1d / 5d) by
        what the stock actually did, crediting patterns + weights + the P(win) model at each one. Short
        horizons are noisier so they carry a lighter credit rate — but they let the brain start learning
        from a setup within HOURS instead of waiting a full hold. An obs is dropped once its longest
        horizon resolves. This is the main lever that gets mastery off zero faster."""
        from . import prices
        still, learned, dropped = [], 0, 0
        for o in self.obs:
            done = set(o.get("done", []))
            age_h = (time.time() - o["ts"]) / 3600.0
            # AUDIT FIX: when SEVERAL horizons matured at once (weekend backlog, resolution gaps), the
            # old loop credited each of them AT THE SAME PRICE — one observation became 3 identical
            # labels (triple-training the model/patterns/weights on one fact). Now: credit only the
            # LONGEST matured horizon; the shorter ones are marked done without credit.
            matured = [(h, r) for h, r in self._horizons if h not in done and age_h >= h]
            if not matured or learned >= max_per_cycle:
                o["done"] = sorted(done)
                if len(done) < len(self._horizons):
                    still.append(o)
                continue
            px = prices.last_price(o["ticker"])
            if px and o.get("entry"):
                h, rate = matured[-1]                                   # the longest matured horizon
                ret = (px / o["entry"] - 1) * 100
                mkt = prices.market_return_since(o["ts"])               # ALPHA-label observations too
                alpha = ret - mkt if mkt is not None else ret
                win = alpha > 0
                self.patterns.record(o.get("sig", ""), win, alpha,
                                     grad=False)   # observations train patterns, NOT the keep/cut ledger
                self._credit_sources(o["sources"], win, alpha, rate=rate)  # horizon-scaled, < a real trade
                if self.lgate is not None and o.get("feat"):
                    # W2.1 PER-HORIZON HEADS — the root-cause fix for 12k-samples-zero-skill. One
                    # blended label mixed horizons whose real base rates are ~0.15 (1h) / 0.45 (24h) /
                    # 0.55 (120h): a horizon-blind model's best output is the blend, pinning Brier at
                    # the no-skill floor. Route each label to the head that owns its horizon; the ~1h
                    # probe NEVER trains a model (near-zero drift = label noise) — it still credits
                    # patterns/sources above at its light rate.
                    if h >= 100:
                        self._head("model_h120").update(o["feat"], win)   # the real hold-horizon label
                    elif h >= 12:
                        self.lgate.update(o["feat"], win)                 # legacy "model" = the 24h head
                    self.know.setdefault("diag", {})["obs_updates"] = \
                        self.know.get("diag", {}).get("obs_updates", 0) + 1
                elif o.get("feat"):
                    # DIAGNOSTIC (2026-07-06): ledger rows advanced while model.n froze — if this ever
                    # fires, labels are flowing but NOT training P(win); scream so the watch sees it.
                    self.know.setdefault("diag", {})["obs_skipped_no_lgate"] = \
                        self.know.get("diag", {}).get("obs_skipped_no_lgate", 0) + 1
                    if not getattr(self, "_lgate_warned", False):
                        self._lgate_warned = True
                        print("  ⚠ resolve_observations: learner.lgate is None — labels NOT training P(win)!")
                self._ledger_resolved(o, h, alpha, win, age_h=age_h)
                for hh, _ in matured:                                    # shorter ones: done, no credit
                    done.add(hh)
                learned += 1
            elif age_h >= self._horizons[-1][0] + OBS_GRACE_H:
                # TERMINAL STATE (W1.1): past every horizon + grace and STILL no price/entry (delisted,
                # bad symbol, feed gap) -> DROP for good instead of re-queueing forever. These stuck
                # unresolvables once pinned the buffer at cap so every new setup was rejected.
                dg = self.know.setdefault("diag", {})
                dg["obs_unresolvable"] = dg.get("obs_unresolvable", 0) + 1
                dropped += 1
                continue
            o["done"] = sorted(done)
            if len(done) < len(self._horizons):
                still.append(o)                                          # keep until the last horizon lands
        self.obs = still
        if learned or dropped:
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
