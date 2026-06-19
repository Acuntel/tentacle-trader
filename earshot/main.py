"""EARSHOT orchestrator. Polls the tentacles -> scores -> gates -> (shadow) executes -> learns.

Run:
    python -m earshot.main --once     # one cycle (great for testing)
    python -m earshot.main --loop     # keep scouring on config.sources.poll_seconds
"""
from __future__ import annotations

import argparse
import os
import time

import yaml

from . import prices
from .sources import build_sources
from .brain import Brain
from .survival import MotherInstinct
from .risk import RiskGate
from .learning import Learner
from .executor import get_executor
from .scout import SourceScout
from .daily import DailyGuard


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    try:  # optional .env
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    cfg["_user_agent"] = os.getenv("EARSHOT_USER_AGENT", "EARSHOT research bot (contact: set EARSHOT_USER_AGENT)")
    for d in ("state", "dungeon"):
        os.makedirs(d, exist_ok=True)
    return cfg


class Earshot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.weights = dict(cfg["source_weights"])
        self.sources = build_sources(cfg, self.weights)
        self.brain = Brain(cfg)
        self.instinct = MotherInstinct(cfg)
        self.risk = RiskGate(cfg)
        self.learner = Learner(self.weights)
        self.executor = get_executor(cfg["identity"]["mode"])
        self.scout = SourceScout(self.learner.know, self.weights)
        self.daily = DailyGuard(cfg)
        self.base_min = cfg["conviction"]["min_score_to_trade"]

    def _equity_now(self) -> float:
        """Realized paper equity + unrealized mark-to-market of open positions (today's true damage)."""
        eq = self.instinct.state["paper_equity"]
        for p in self.learner.open:
            px = prices.last_price(p["ticker"])
            if px and p.get("entry_price"):
                eq += p["size_usd"] * (px / p["entry_price"] - 1)
        return eq

    def run_once(self):
        st = self.instinct.status()
        print("\n" + "=" * 74)
        print(f"EARSHOT [{st['mode'].upper()}]  rung {st['rung']} ({st['stage']})  "
              f"capital-unlocked ${st['capital_unlocked']}  mastery {st['mastery']}/{st['needs_mastery']}")
        print(f"mood: {st['mood']}")
        print(f"tentacles live: {', '.join(s.key for s in self.sources) or '(none)'}"
              f"{'' if prices.have_prices() else '   [no yfinance: price filters degrade gracefully]'}")
        print("-" * 74)

        # 1) scour
        signals, seen = [], set()
        for s in self.sources:
            got = s.safe_fetch()
            print(f"  {s.key:>13}: {len(got)} signal(s)")
            for sig in got:
                k = (sig.source, tuple(sig.tickers), sig.headline)
                if k not in seen:
                    seen.add(k)
                    signals.append(sig)

        # 2) learn from anything that has matured FIRST (so realized equity is current)
        for r in self.learner.resolve_due(self.instinct):
            o, m = r["outcome"], r["mind"]
            tag = "WIN " if o["win"] else "LOSS"
            print(f"  [{tag}] {o['ticker']} {o['pnl_pct']:+.1f}% (${o['pnl_usd']:+.2f}) "
                  f"felt {m['felt']:+.1f} | {m['note']}")

        # 3) DAILY CIRCUIT BREAKER — realized + unrealized mark-to-market
        capital = self.instinct.sizing_capital()
        equity_now = self._equity_now()
        newly_breached = self.daily.update(equity_now, capital)
        day_pnl = self.daily.day_pnl(equity_now)
        # caution ratchets the conviction bar up after bad days, decays on clean ones
        eff_min = self.base_min + self.daily.caution_bump()
        self.brain.conv["min_score_to_trade"] = eff_min
        limit = self.daily.loss_pct * capital
        print(f"\n  day P&L ${day_pnl:+.2f} / limit -${limit:.0f}   conviction bar {eff_min:.0f} "
              f"(caution +{self.daily.caution_bump():.0f})")
        if newly_breached:
            reflection = self.learner.deep_daily_reflection(day_pnl, self.learner.open)
            print("\n  !! DAILY -10% CIRCUIT BREAKER TRIPPED — HALTING FOR THE DAY !!")
            print("  mood: ASHAMED — I hurt the child badly and treated the money like nothing.")
            print(f"  dungeon: {reflection}")

        # 4) think
        regime_down = bool(self.cfg["risk"].get("regime_filter")) and prices.regime_downtrend(
            self.cfg["regime"]["proxy"], self.cfg["regime"]["downtrend_lookback_days"])
        cands = self.brain.evaluate(signals, capital, regime_down)
        print(f"\n  {len(signals)} signals -> {len(cands)} tickers in view"
              f"{'   [REGIME: downtrend, longs suppressed]' if regime_down else ''}")
        for c in cands[:5]:
            print(f"    {c.ticker:<6} score {c.confidence:5.1f}  {c.catalyst.value:<18} "
                  f"{getattr(c, '_families', 1)} fam  ~${c.size_usd:.0f}")

        # 5) act — but NOT if the day is halted (in the dungeon until tomorrow)
        if self.daily.halted():
            print("\n  >> HALTED for the rest of the day (-10% breaker). In the dungeon. "
                  "Back tomorrow, more cautious.")
        else:
            chosen, why = None, "nothing cleared the bar — staying patient protects the child"
            for c in cands:
                ok, reason = self.brain.passes_gate(c)
                if not ok:
                    why = f"{c.ticker}: {reason}"; continue
                ok, reason = self.risk.check(c, self.instinct, len(self.learner.open), day_pnl)
                if not ok:
                    why = f"{c.ticker}: {reason}"; continue
                chosen = c; break
            if chosen:
                fill = self.executor.buy(chosen)
                self.risk.on_execute()
                self.learner.open_position(chosen, fill["price"])
                print(f"\n  >> SHADOW BUY {chosen.ticker}  ~${chosen.size_usd:.0f} @ ${fill['price']:.2f}  "
                      f"(score {chosen.confidence:.0f}, {chosen.catalyst.value})")
                print(f"     conviction: {chosen.reasons[0]}")
            else:
                print(f"\n  >> NO TRADE — {why}")

        # 6) scout + INTEGRITY AUDIT (recompute the score from the immutable ledger)
        rep = self.scout.report()
        if rep["sources"]:
            print("\n  source scout:", ", ".join(f"{r['source']}={r['precision']}" for r in rep["sources"][:6]))
            if rep["decaying"]:
                print(f"    decaying -> {rep['decaying']}; would propose:",
                      [s for s, _ in self.scout.propose('smart_money')][:2])
        a = self.instinct.audit()
        if not a.get("ok"):
            print(f"  ⚠ INTEGRITY ALERT — ledger/state mismatch: {a}")
        elif a.get("n"):
            print(f"  integrity OK — score verified against {a['n']} immutable ledger outcomes "
                  f"(equity ${a['ledger_equity']}, mastery {a['ledger_mastery']})")
        print("=" * 74)

    def loop(self):
        every = int(self.cfg["sources"].get("poll_seconds", 60))
        print(f"EARSHOT looping every {every}s. Ctrl-C to stop.")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\nstopped."); break
            except Exception as e:
                print(f"cycle error: {e}")
            time.sleep(every)


def main():
    ap = argparse.ArgumentParser(prog="earshot")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="run a single cycle (this is the default)")
    ap.add_argument("--loop", action="store_true", help="keep scouring (default: one cycle)")
    args = ap.parse_args()
    bot = Earshot(load_config(args.config))
    bot.loop() if args.loop else bot.run_once()


if __name__ == "__main__":
    main()
