"""TENTACLE orchestrator. Polls the tentacles -> scores -> gates -> (shadow) executes -> learns.

Run:
    python -m earshot.main --once     # one cycle (great for testing)
    python -m earshot.main --loop     # keep scouring on config.sources.poll_seconds
"""
from __future__ import annotations

import argparse
import os
import time

import yaml

from . import prices, market, history, warmstart
from .sources import build_sources
from .approvals import ApprovalQueue
from .brain import Brain
from .survival import MotherInstinct
from .risk import RiskGate
from .learning import Learner
from .executor import get_executor
from .scout import SourceScout
from .daily import DailyGuard
from .exits import ExitEngine
from .macro import MacroMonitor
from .enrich import EnrichmentEngine
from .selfreview import SelfReview
from .analyst import Analyst
from .learnedgate import LearnedGate
from .histtrain import HistoricalTrainer
from .sim import SimEngine
from .coach import Coach
from .audit import Auditor
from .core import BrainCore


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    try:  # optional .env
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    cfg["_user_agent"] = os.getenv("TENTACLE_USER_AGENT", "TENTACLE research bot (contact: set TENTACLE_USER_AGENT)")
    # shared-bot mode (texting THROUGH Nessobot): SEND only, never poll its token -> Nesso keeps working
    if cfg.get("telegram", {}).get("shared_bot") and not os.getenv("TELEGRAM_SHARED_BOT"):
        os.environ["TELEGRAM_SHARED_BOT"] = "1"
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
        self.exits = ExitEngine(cfg)
        self.explore_cfg = cfg.get("exploration", {})          # WS1: off-book learning-trade flood
        self.base_min = cfg["conviction"]["min_score_to_trade"]
        self.buffer: list = []                                  # rolling signal memory
        self.window = int(cfg["sources"].get("signal_window_sec", 1800))
        self.macro_mon = MacroMonitor(                          # FRED alt-data / macro regime
            expand_cap=int(cfg["sources"].get("fred_expand_cap", 0)))
        self.brain.patterns = self.learner.patterns            # share the learned pattern memory
        self.analyst = Analyst(cfg)                            # reasoning judge (heuristic / Claude)
        self.lgate = LearnedGate(self.learner.know)            # learned P(win) over the dossier
        self.brain.analyst = self.analyst
        self.brain.lgate = self.lgate
        self.histtrain = HistoricalTrainer(cfg, self.learner.know)   # #1 decade->live transfer-gap prior
        self.brain.hist_gate = self.histtrain.gate
        self.learner.lgate = self.lgate                       # so resolved outcomes train the model
        self.core = BrainCore(self.learner.know, self.weights, cfg)  # THE shared brain — wired to everything
        self.brain.core = self.core                           # learned trading rules + calibration in decisions
        self.learner.core = self.core                         # lead-time learning on every confirmed setup
        self.enricher = EnrichmentEngine(cfg)                  # on-demand data collection
        self.enricher.weights = self.weights
        self.selfreview = SelfReview(cfg, self.learner, self.instinct)  # continuous self-diagnosis
        self.sim = SimEngine(cfg, self.learner)                # 24/7 backtests on the cached decade
        self.approvals = ApprovalQueue(cfg, self.learner.know, self.learner._persist_know)  # Telegram approve/deny
        self.coach = Coach(cfg, self.learner, self.instinct, self.sim, self.scout, self.core,
                           self.approvals)                     # self-improvement -> proposals to Telegram
        self.auditor = Auditor(cfg)                            # 24/7 self-code audit loop (propose-only)
        # progressive multi-horizon resolution (1h/1d/5d): learning accrues in HOURS, not only after a hold
        lc = cfg.get("learning", {})
        self.learner.set_horizons(lc.get("horizons_hours", [24]), lc.get("horizon_credit", [0.05]))
        # warm-start the P(win) model so a fresh volume isn't a cold start (mastery is NOT warm-started)
        try:
            ws = warmstart.run(self.lgate, self.learner, self.sim, cfg)
            print(f"  warm-start: {ws}")
        except Exception as e:
            print(f"  warm-start skipped: {e}")
        self.approvals.startup_ping(
            f"rung {self.instinct.status()['rung']} {self.instinct.status()['mode']} · "
            f"P(win) model {self.lgate.n} samples · I'll text you any self-change for ✅/❌ before it ships.")
        self.review = cfg.get("selfreview", {})
        self.hist_every = int(cfg.get("sim", {}).get("history_refresh_seconds", 120))
        self.hist_slice = int(cfg.get("sim", {}).get("history_slice", 25))
        self.hist_years = int(cfg.get("sim", {}).get("history_years", 10))
        self.last_hist = 0.0
        self.cycle = 0
        self.last_review_day = None

    def _dedup(self) -> list:
        seen, out = set(), []
        for sg in reversed(self.buffer):
            k = (sg.source, tuple(sg.tickers), sg.headline)
            if k not in seen:
                seen.add(k)
                out.append(sg)
        return out

    def _equity_now(self) -> float:
        """Realized paper equity + unrealized mark-to-market of open positions (today's true damage)."""
        eq = self.instinct.state["paper_equity"]
        for p in self.learner.open:
            px = prices.last_price(p["ticker"])
            if px and p.get("entry_price"):
                eq += p["size_usd"] * (px / p["entry_price"] - 1)
        return eq

    def run_once(self):
        self.cycle += 1
        self.analyst.reset_cycle()                             # refresh the per-cycle LLM budget
        st = self.instinct.status()
        mkt_open, mkt_why, mkt = market.can_open(self.cfg.get("market", {}))
        print("\n" + "=" * 74)
        print(f"TENTACLE [{st['mode'].upper()}]  rung {st['rung']} ({st['stage']})  "
              f"capital-unlocked ${st['capital_unlocked']}  mastery {st['mastery']}/{st['needs_mastery']}")
        goal = self.cfg.get("survival", {}).get("goal_monthly_return_pct", 10)
        mr = st.get("monthly_return")
        mr_str = f"{mr:+.1f}%/mo realized" if mr is not None else "warming up (need ~3d of trades)"
        print(f"scorecard: {mr_str}  vs goal +{goal}%/mo   |   "
              f"samples {st['samples']}/{self.instinct.rung_cfg()['min_samples']}")
        print(f"mood: {st['mood']}")
        nxt = "" if mkt_open else f"  | next open {mkt['next_open']}"
        print(f"market: {'OPEN' if mkt_open else 'CLOSED'} — {mkt['reason']}  ({mkt['et']}){nxt}")
        print(f"tentacles live: {', '.join(s.key for s in self.sources) or '(none)'}"
              f"{'' if prices.have_prices() else '   [no yfinance: price filters degrade gracefully]'}")
        print(f"brain: {self.analyst.brain_name()}"
              f"  |  learned P(win) model: {self.lgate.n} samples"
              f"{' (gating)' if self.lgate.ready() else ' (advisory until 30)'}")
        # WS6 LEARNING VELOCITY — the headline metric: is it actually learning, and how fast?
        vel = self.core.velocity(self.lgate)
        b7 = vel["brier_7d"]
        btrend = (f"7d {b7:+.3f} improving" if vel["learning"] else
                  f"7d {b7:+.3f} FLAT" if (b7 is not None and vel["no_edge"]) else
                  f"7d {b7:+.3f}" if b7 is not None else "building trend")
        ex = (f"explore {vel['explore_trades']} trades, dir-hit "
              f"{round(100*vel['dir_hit'])}%, alpha {vel['avg_explore_alpha']:+.2f}%"
              if vel["dir_hit"] is not None else "explore: warming up")
        spd = f"+{vel['samples_per_day']}/day" if vel["samples_per_day"] is not None else "samples/day building"
        print(f"learning velocity: P(win) n={vel['n']} brier {vel['brier']} [{btrend}] trust {vel['trust']}  |  "
              f"{ex}  |  {spd}")
        if vel["no_edge"]:
            print("  ⚠ HONEST: thousands of samples in, calibration still flat — NO EDGE DETECTED "
                  "in this configuration (faster learning has bought a fast, confident 'no').")
        print("-" * 74)

        # 0) HUMAN-IN-THE-LOOP — handle any Telegram approve/deny taps (approved patches -> apply Action)
        for line in self.approvals.process():
            print(f"  {line}")

        # 1) scour — only poll tentacles that are DUE (each at its own rate-limit-safe cadence)
        now = time.time()
        polled = []
        for s in self.sources:
            if not s.due():
                continue
            got = s.safe_fetch()
            polled.append(f"{s.key}:{len(got)}")
            self.buffer.extend(got)
        # rolling memory: keep the recent window so async polls still confirm each other
        self.buffer = [sg for sg in self.buffer if now - sg.ts <= self.window]
        signals = self._dedup()
        print(f"  polled: {' '.join(polled) or '(nothing due this tick)'}")
        print(f"  signal memory: {len(signals)} live signals (last {self.window // 60}m)")

        # 24/7 LEARNING — keep loading the decade (incremental) and simulate on it every cycle,
        # market open or closed. Live TRADING waits for the open; LEARNING never stops.
        now2 = time.time()
        if history.have_yf() and now2 - self.last_hist >= self.hist_every:
            self.last_hist = now2
            cov = history.ensure_slice(self.hist_years, self.hist_slice)
            print(f"  history: {cov['have']}/{cov['total']} names hold {self.hist_years}y of bars "
                  f"(+{cov['fetched']} this top-up)")
        simr = self.sim.step()
        if simr.get("ran"):
            sr = self.sim.report()
            print(f"  sims: +{simr['ran']} replays (total {sr['episodes']}, win {sr['win_rate']}, "
                  f"avg {sr['avg_ret']}%) — sim-son ${sr['sim_equity']} ({sr['grown_pct']:+.1f}%)")
        ht = self.histtrain.step()                      # #1 train the decade->live P(win) transfer prior
        if ht.get("trained"):
            print(f"  histtrain: +{ht['trained']} decade alpha-events -> P(win) prior "
                  f"(n={ht['total']}, brier {ht.get('brier', '?')})")

        # 2) MANAGE open positions against the live price FIRST (exit engine decides sells)
        for r in self.learner.resolve_due(self.instinct, self.exits):
            o, m = r["outcome"], r["mind"]
            tag = "WIN " if o["win"] else "LOSS"
            print(f"  [SELL/{r['exit']}] {tag} {o['ticker']} {o['pnl_pct']:+.1f}% "
                  f"(${o['pnl_usd']:+.2f}) | felt {m['felt']:+.1f} | {m['note']}")
        # LEARN FROM ALL DATA: label matured observations (confirmed setups we saw, traded or not)
        learned = self.learner.resolve_observations()
        if learned:
            print(f"  learned from {learned} matured observations (data, not just trades)")
        # WS1: resolve OFF-BOOK learning trades (stop/target/time) — floods patterns/P(win)/weights,
        # never touches mastery or the $500. Resolves every cycle (market open or closed, like exits).
        if self.explore_cfg.get("enabled", True) and self.brain.explore:
            exr = self.learner.resolve_learning(int(self.explore_cfg.get("max_open", 300)),
                                                float(self.explore_cfg.get("credit_rate", 0.10)))
            if exr["resolved"]:
                hit = round(100 * exr["dir_hits"] / exr["resolved"])
                print(f"  [explore] resolved {exr['resolved']} off-book learning trades "
                      f"(dir-hit {hit}%, {exr['open']} still open)")

        # 3) DAILY CIRCUIT BREAKER — realized + unrealized mark-to-market
        capital = self.instinct.sizing_capital()
        equity_now = self._equity_now()
        newly_breached = self.daily.update(equity_now, capital)
        day_pnl = self.daily.day_pnl(equity_now)
        # caution ratchets the conviction bar up after bad days, decays on clean ones
        fear_caution = self.instinct.protective_caution()       # love -> caution: fear tightens entries near death
        eff_min = self.base_min + self.daily.caution_bump() + fear_caution
        self.brain.conv["min_score_to_trade"] = eff_min
        limit = self.daily.loss_pct * capital
        print(f"\n  day P&L ${day_pnl:+.2f} / limit -${limit:.0f}   conviction bar {eff_min:.0f} "
              f"(caution +{self.daily.caution_bump():.0f}, fear +{fear_caution:.0f})")
        if newly_breached:
            reflection = self.learner.deep_daily_reflection(day_pnl, self.learner.open)
            print("\n  !! DAILY -10% CIRCUIT BREAKER TRIPPED — HALTING FOR THE DAY !!")
            print("  mood: ASHAMED — I hurt the child badly and treated the money like nothing.")
            print(f"  dungeon: {reflection}")

        # 4) think — refresh the macro regime (daily-throttled) and feed it to the brain
        self.macro_mon.update()
        self.brain.macro = self.macro_mon.context
        # wire the live signal STREAM + macro REGIME into the shared brain, stored over time
        from collections import Counter
        _day = time.strftime("%Y-%m-%d")
        _top = [t for t, _ in Counter(tk for sg in signals for tk in sg.tickers).most_common(15)]
        self.core.record_stream(_day, sorted({s.source for s in signals}), len(signals), _top)
        self.core.record_regime(_day, self.macro_mon.context or "n/a")
        regime_down = bool(self.cfg["risk"].get("regime_filter")) and prices.regime_downtrend(
            self.cfg["regime"]["proxy"], self.cfg["regime"]["downtrend_lookback_days"])
        cands = self.brain.evaluate(signals, capital, regime_down)
        # on-demand enrichment: pull more data on under-confirmed names, then re-evaluate
        added = self.enricher.enrich(cands, self.buffer)
        if added:
            signals = self._dedup()
            cands = self.brain.evaluate(signals, capital, regime_down)
        print(f"\n  macro regime: {self.macro_mon.context or 'pending'}   "
              f"{len(signals)} signals -> {len(cands)} tickers in view"
              f"{f'   [enriched +{added}]' if added else ''}"
              f"{'   [REGIME: downtrend, longs suppressed]' if regime_down else ''}")
        for c in cands[:5]:
            print(f"    {c.ticker:<6} score {c.confidence:5.1f}  {c.catalyst.value:<18} "
                  f"{getattr(c, '_families', 1)} fam  ~${c.size_usd:.0f}")
        self.learner.observe(cands)        # every confirmed setup -> a labeled example to learn from

        # 5) act — but NOT if the day is halted, and NEVER when the market is closed (can't trade it)
        if self.daily.halted():
            print("\n  >> HALTED for the rest of the day (-10% breaker). In the dungeon. "
                  "Back tomorrow, more cautious.")
        elif not mkt_open:
            print(f"\n  >> NO ENTRIES — {mkt_why}. "
                  "Open positions still managed; stops/exits stay armed for the next session.")
        else:
            chosen, why = None, "nothing cleared the bar — staying patient protects the child"
            blocks = []                       # (ticker, reason) for names that didn't clear, BEST-first
            for c in cands:                   # cands is sorted by confidence desc -> blocks[0] is the top pick
                ok, reason = self.brain.passes_gate(c)
                if not ok:
                    blocks.append((c.ticker, reason)); continue
                ok, reason = self.risk.check(c, self.instinct, self.learner.open, day_pnl)
                if not ok:
                    blocks.append((c.ticker, reason)); continue
                chosen = c; break
            if not chosen and blocks:
                why = f"{blocks[0][0]}: {blocks[0][1]}"   # the TOP candidate's REAL blocker, not the last one's
            # capital-aware sizing: never deploy more than what's left unspent
            deployed = sum(p["size_usd"] for p in self.learner.open)
            available = max(0.0, capital - deployed)
            if chosen and chosen.size_usd > available:
                chosen.size_usd = round(available, 2)
            if chosen and chosen.size_usd < 25:
                chosen, why = None, f"capital fully deployed (${deployed:.0f}/${capital:.0f}) — holding"
            if chosen:
                fill = self.executor.buy(chosen)
                self.risk.on_execute()
                self.learner.open_position(chosen, fill["price"])
                print(f"\n  >> SHADOW BUY {chosen.ticker}  ~${chosen.size_usd:.0f} @ ${fill['price']:.2f}  "
                      f"(score {chosen.confidence:.0f}, {chosen.catalyst.value})")
                print(f"     conviction: {chosen.reasons[0]}")
                print(f"     plan: {chosen.reasons[2]} | pattern {getattr(chosen, '_signature', 'new')}")
            else:
                print(f"\n  >> NO TRADE — {why}")
                for tk, rsn in blocks[:5]:    # why the BEST candidates didn't fire (real per-name reasons)
                    print(f"       · {tk}: {rsn}")

            # WS1 FLOOD: every QUALIFYING name (not just the one conviction pick) becomes an off-book
            # long/short paper learning trade — ignoring the bar/families/regime/capital. Feeds the
            # learners hundreds of forward labels/day; never touches mastery or the real $500.
            if self.explore_cfg.get("enabled", True) and self.brain.explore:
                picks = self.brain.learning_picks(
                    cands, int(self.explore_cfg.get("top_k", 20)),
                    float(self.explore_cfg.get("short_pwin", 0.45)),
                    bool(self.explore_cfg.get("allow_short", True)))
                notional = float(self.explore_cfg.get("notional_usd", 100))
                opened = longs = shorts = 0
                for c, d in picks:
                    px = prices.last_price(c.ticker)          # entry price only — no executor side effects
                    if px and self.learner.open_learning(c, px, d, notional):
                        opened += 1
                        longs += 1 if d > 0 else 0
                        shorts += 1 if d < 0 else 0
                if opened:
                    print(f"  [explore] +{opened} off-book learning trades ({longs}L/{shorts}S) "
                          f"-> feeds patterns/P(win)/weights, never touches mastery or $")

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
        prep = self.learner.patterns.report()
        if prep["best"]:
            print("  patterns learned — best:", [f"{r['sig']}={r['win']}({r['n']})" for r in prep["best"][:3]])
            if prep["worst"]:
                print("    vetoed/weak:", [f"{r['sig']}={r['win']}({r['n']})" for r in prep["worst"][:3]])

        # 7) SELF-REVIEW — diagnose its own behaviour and recommend (auto-apply the safe) fixes
        every = max(1, int(self.review.get("every_cycles", 240)))
        day = time.strftime("%Y-%m-%d")
        if self.review.get("enabled", True) and (
                self.cycle == 1 or self.cycle % every == 0 or day != self.last_review_day):
            self.last_review_day = day
            rep = self.selfreview.run()
            print(f"\n  self-review — {rep['n_auto_applied']} safe fix(es) auto-applied:")
            for line in SelfReview.summarize(rep):
                print(f"    - {line}")

        # 8) SELF-IMPROVEMENT (propose-only) — fuse hard stats + the free LLM to tune weights live,
        # write lessons, and JOURNAL proposed code changes for Brady to review. Runs 24/7 on cadence.
        if self.coach.due():
            cr = self.coach.run(signals, self.macro_mon.context)
            print(f"\n  coach [{cr['by']}] — {self.coach.last_summary}")
            if not cr["redteam"]["ok"]:
                print(f"    ⚠ SAFETY RED-TEAM FAILED: {cr['redteam']['failed']} — a protective gate no longer holds!")
            if cr["code_proposals"] or cr.get("new_sources"):
                print(f"    {len(cr['code_proposals'])} code + {len(cr.get('new_sources', []))} new-source "
                      "idea(s) journaled -> state/evolution/ (review); diary in EVOLUTION_LOG.md")

        # 9) 24/7 SELF-CODE AUDIT — read & critique its own source on loop, journal fixes (propose-only)
        if self.auditor.due():
            ar = self.auditor.run()
            broke = f" ⚠ {len(ar['broken'])} BROKEN" if ar.get("broken") else ""
            print(f"\n  self-audit [{ar.get('by', 'static')}]: reviewed earshot/{ar['module']} — "
                  f"{ar['findings']} finding(s) journaled{broke}")
        print("=" * 74)

    def loop(self):
        every = int(self.cfg["sources"].get("poll_seconds", 60))
        print(f"TENTACLE looping every {every}s. Ctrl-C to stop.")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\nstopped."); break
            except Exception as e:
                import traceback
                print(f"cycle error: {e}")
                print(traceback.format_exc())
            time.sleep(every)


def main():
    ap = argparse.ArgumentParser(prog="tentacle")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="run a single cycle (this is the default)")
    ap.add_argument("--loop", action="store_true", help="keep scouring (default: one cycle)")
    args = ap.parse_args()
    bot = Earshot(load_config(args.config))
    bot.loop() if args.loop else bot.run_once()


if __name__ == "__main__":
    main()
