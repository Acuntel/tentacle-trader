"""The 24/7 coach — the mother's heartbeat for growing her son, wired to the WHOLE brain.

Every pass it (1) runs the SIMULTANEOUS FUSION across the shared core — a source's live weight becomes
its precision x how early it fires x whether its patterns decayed, so every signal sharpens the others;
(2) runs all six self-improvement engines (rule optimization, catalyst studies, calibration, red-team,
SPY benchmark) — each writing into that same core; and (3) hands the LLM the ENTIRE brain snapshot so it
reasons across everything at once and proposes more. Weight/lesson tuning is auto-applied (params only);
code changes stay PROPOSALS for Brady to review. Nothing learns in a silo; it all compounds.
"""
from __future__ import annotations

import json
import os
import time

from . import llm, evolve, persist, selfcode
from .learning import WMIN, WMAX
from .models import iso
from .strategies import StrategyBook
from .proposer import Proposer
from .council import Hive
from .forge import Forge

EVO_DIR = "state/evolution"
LOG_MD = os.path.join(EVO_DIR, "EVOLUTION_LOG.md")
PROPOSALS = os.path.join(EVO_DIR, "code_proposals.jsonl")

SYSTEM = ("You are a mother raising your SON — your trading capital. Two drives: grow him as LARGE as "
          "he can possibly be (train him to his best), and NEVER let him die (loss hurts far more than "
          "gain). You are looking at your son's ENTIRE brain — every source's track record and lead-time, "
          "learned trading rules, pattern win-rates and decay, calibration, the decade of sims/studies, "
          "the SPY benchmark, and the safety red-team — all at once. Decide how to improve. You MAY retune "
          "which sources to trust and note lessons; you may PROPOSE code for a human to review; you can "
          "NEVER weaken the hard caps, the real-money gate, or the Gauntlet. Reply ONLY as a JSON object.")


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Coach:
    def __init__(self, cfg, learner, instinct, sim, scout=None, core=None, approvals=None):
        c = cfg.get("coach", {})
        self.cfg = cfg
        self.on = bool(c.get("enabled", True))
        self.every = int(c.get("every_minutes", 30)) * 60
        self.learner = learner
        self.instinct = instinct
        self.sim = sim
        self.scout = scout
        self.core = core
        self.approvals = approvals                         # Telegram approve/deny queue (optional)
        self.proposer = Proposer(cfg, learner.know, approvals) if approvals is not None else None
        # the recursive multi-model agent council (agents working tasks + spinning up more tasks, 24/7)
        self.hive = (Hive(cfg, learner.know, approvals, self.proposer, persist=learner._persist_know)
                     if approvals is not None else None)
        # the Forge — free deterministic recursive search engine (the keystone); LLM only on plateau
        self.forge = (Forge(cfg, learner.know, self.proposer, persist=learner._persist_know)
                      if self.proposer is not None else None)
        self.book = StrategyBook(cfg, core) if core is not None else None   # the strategy tournament
        self.last = 0.0
        self.last_backup_day = None
        self.know = learner.know
        self.know.setdefault("lessons", [])
        self.know.setdefault("coach_notes", {})
        self.last_summary = "coach has not run yet"

    def due(self) -> bool:
        return self.on and self.core is not None and (time.time() - self.last) >= self.every

    def _report(self, news_signals, macro_ctx) -> dict:
        heads, seen = [], set()
        for sg in reversed(news_signals or []):
            h = getattr(sg, "headline", "")
            if h and h not in seen:
                seen.add(h); heads.append(h)
            if len(heads) >= 12:
                break
        try:                                               # collapse echoed copies of the same event (BGE-M3)
            from . import embed
            if embed.available() and len(heads) > 3:
                heads = [heads[i] for i in embed.dedupe(heads, threshold=0.86)]
        except Exception:
            pass
        losses = []
        try:
            with open("state/dungeon/post_mortems.jsonl") as f:
                losses = [json.loads(x)["root_cause"] for x in f.readlines()[-5:]]
        except Exception:
            pass
        rep = {"creed": self.instinct.creed(), "mood": self.instinct.mood(),
               "emotion": self.instinct.emotion(), "ambition": self.instinct.ambition(),
               "scale_tier": self.instinct.scale_tier(),
               "mastery": self.instinct.mastery(), "growth_progress": self.instinct.growth_progress(),
               "sim": self.sim.report(), "macro_regime": macro_ctx,
               "strategy_leaderboard": self.book.leaderboard(6) if self.book else [],
               "honesty_out_of_sample": self.know.get("honesty", {}),
               "live_strategy": self.know.get("champion_strategy", {}),
               "recent_losses": losses, "tonight_news": heads}
        rep.update(self.core.snapshot())                  # the WHOLE shared brain, in one view
        return rep

    def run(self, news_signals=None, macro_ctx="") -> dict:
        self.last = time.time()
        patterns = self.learner.patterns

        # (1) SIMULTANEOUS FUSION — weights = precision x lead-time x decay, across the whole brain
        syn = self.core.synthesize(patterns)
        # (2) the six self-improvement engines, all writing into the shared core
        cal = evolve.update_calibration(self.core)
        rules = evolve.optimize_rules(self.core, self.cfg)
        studies = evolve.run_studies(self.core, patterns)
        rt = evolve.redteam(self.cfg); self.know["redteam"] = rt
        bench = evolve.benchmark(self.core, self.sim, int(self.cfg.get("sim", {}).get("history_years", 10)))
        # STRATEGY TOURNAMENT — N strategies compete on the decade; adopt the champion's rules as live
        tour = self.book.evaluate() if self.book else {"ran": 0}
        champ = self.book.adopt_blend(self.cfg) if self.book else None       # MIXTURE of out-of-sample survivors -> live
        evo = self.book.evolve_book() if self.book else {"evolved": 0}       # strategies CHANGE (breed best, retire worst)
        honesty = self.book.honesty() if self.book else {}                   # never let it believe its own backtest
        self.know["honesty"] = honesty

        # RECURSIVE GROWTH LEDGER — snapshot the whole son today, track whether he's growing FASTER,
        # and back the brain up daily so an update can never lose progress.
        day = time.strftime("%Y-%m-%d")
        sr = self.sim.report()
        lg = getattr(self.learner, "lgate", None)                       # WS6: track learning velocity over days
        self.core.record_day(day, {
            "mastery": self.instinct.mastery(), "sim_son": sr["sim_equity"], "sim_win": sr["win_rate"],
            "n_patterns": len(self.know["patterns"]), "edge": bench.get("edge_per_trade_pct", 0),
            "calib": cal.get("factor", 1.0), "n_outcomes": cal.get("n", 0),
            "samples": int(getattr(lg, "n", 0)), "brier": round(float(getattr(lg, "brier", 0.25)), 4),
            "growth": self.instinct.growth_progress()})
        if day != self.last_backup_day:
            persist.backup("state/knowledge.json", day)
            try:                                           # off-volume backup to GitHub — never lose the brain
                from . import backup as _bk
                bcfg = self.cfg.get("backup", {})
                if bcfg.get("enabled", True) and bcfg.get("github_repo"):
                    _bk.push_state(bcfg.get("github_repo"))
            except Exception:
                pass
            self.last_backup_day = day

        # (3) the LLM reasons across the ENTIRE brain snapshot + the mother's creed + her feelings
        rep = self._report(news_signals, macro_ctx)
        traj = rep.get("trajectory", {})
        tuned, lessons, code_props, watch, cfg_props = (
            ["synthesis"] if syn.get("weights_moved") else [], [], [], [], [])
        v = llm.ask_json(SYSTEM,
                         "Review your son's WHOLE brain and his growth trajectory. Your goal is recursive: "
                         "make him grow FASTER than he grew yesterday, every day, without ever risking his "
                         "life. Propose improvements as JSON with keys: "
                         'reweight (object src->"up"|"down"), lessons (array of short strings), '
                         "watchlist (array of tickers), code_proposals (array of {file, change, why}), "
                         "config_changes (array of {setting, from, to, why} — a config.yaml threshold to shift). "
                         "Evidence:\n" + json.dumps(rep, default=str)[:4000],
                         max_tokens=800, priority="normal", task="reason")
        if v:
            for s, dirn in (v.get("reweight") or {}).items():
                if s in self.learner.weights:
                    f = 1.06 if str(dirn).lower().startswith("up") else 0.94
                    nw = round(max(WMIN, min(WMAX, self.learner.weights[s] * f)), 3)
                    self.learner.weights[s] = nw
            lessons = [str(x) for x in (v.get("lessons") or [])][:6]
            watch = [str(x) for x in (v.get("watchlist") or [])][:20]
            code_props = [p for p in (v.get("code_proposals") or []) if isinstance(p, dict)][:6]
            cfg_props = [p for p in (v.get("config_changes") or []) if isinstance(p, dict)][:4]

        for ls in lessons:
            if ls not in self.know["lessons"]:
                self.know["lessons"].append(ls)
        self.know["lessons"] = self.know["lessons"][-100:]
        if watch:
            self.know["coach_notes"]["watchlist"] = watch
        for p in code_props:
            _append(PROPOSALS, {"ts": iso(time.time()), "status": "PROPOSED — review & merge",
                                "file": p.get("file"), "change": p.get("change"), "why": p.get("why")})
        new_sources = []
        if self.scout is not None:
            try:
                new_sources = [s for s, _ in self.scout.propose("smart_money")][:5]
            except Exception:
                new_sources = []
            already = self.know.setdefault("proposed_sources", [])   # propose each idea ONCE, not every cycle
            for name in new_sources:
                if name in already:
                    continue
                already.append(name)
                _append(PROPOSALS, {"ts": iso(time.time()), "status": "NEW SOURCE — consider wiring",
                                    "file": "earshot/sources/", "change": f"add/enable tentacle '{name}'",
                                    "why": "scout: current sources decaying; could sharpen the edge"})
        if self.proposer is not None:
            self.proposer.harvest(rep)                     # load up scored config-tweak ideas (refills when low)
            if code_props:                                 # a gate-ready code idea joins the same ranked pool
                try:
                    patch = selfcode._propose()
                except Exception:
                    patch = None
                if patch and all(k in patch for k in ("file", "find", "replace")):
                    self.proposer.add_code(patch)
            self.proposer.emit_due()                       # send only the single BEST un-sent idea (paced, ≤ cap/day)
        # Capture the agent spin-ups' results — they were appended to last_summary then OVERWRITTEN below
        # (a visibility bug: the Forge + Hive ran every time but you could never SEE their work). Surface
        # them in the FINAL summary instead.
        forge_r = self.forge.tick() if (self.forge is not None and self.forge.due()) else None
        hive_r = self.hive.tick(rep) if (self.hive is not None and self.hive.due()) else None
        self.learner._persist_know()

        if not rt["ok"]:                                  # a safety rail got weakened — scream
            _append(PROPOSALS, {"ts": iso(time.time()), "status": "⚠ SAFETY REGRESSION",
                                "file": "config.yaml / risk", "change": f"red-team FAILED: {rt['failed']}",
                                "why": "a protective gate no longer holds — fix before anything else"})

        self._log(rep, syn, rules, studies, cal, rt, bench, lessons, code_props, new_sources,
                  by="groq+brain" if v else "brain")
        self.last_summary = (
            f"fused {syn['weights_moved']} weights ({syn['decayed_patterns']} decayed patterns), "
            f"rules stop{rules.get('stop_pct', '?')}/tgt{rules.get('target_pct', '?')}/hold{rules.get('hold_days', '?')}, "
            f"calib x{cal.get('factor', 1.0)}, +{len(lessons)} lessons, {len(code_props)} code + "
            f"{len(new_sources)} source ideas, redteam {'OK' if rt['ok'] else 'FAIL ' + str(rt['failed'])}, "
            f"edge/trade vs SPY {bench.get('edge_per_trade_pct', 'n/a')}%, "
            f"blend {str(champ['stop']) + '/' + str(champ['target']) + '/' + str(champ['hold']) + 'd' if champ else 'NONE (nothing survived out-of-sample)'} "
            f"of {len(self.book.book) if self.book else 0} strategies ({evo['evolved']} evolved); "
            f"HONESTY: {honesty.get('status', 'n/a')[:70]}; "
            f"trajectory {'↑ growing faster' if traj.get('improving') else 'flat (day ' + str(traj.get('days', 0)) + ')'}")
        if forge_r:
            self.last_summary += f"\n    🔨 FORGE (free search): {forge_r}"
        if hive_r:
            self.last_summary += f"\n    🐝 HIVE (multi-model agents spinning up tasks): {hive_r}"
        if self.approvals is not None:                       # once/day heartbeat, in plain English
            try:
                stt = self.instinct.status()
            except Exception:
                stt = {}
            pool = self.know.get("proposal_pool", [])
            unsent = [p for p in pool if not p.get("sent")]
            bar = self.proposer.min_impact if self.proposer else 60
            passed = [p for p in unsent if p.get("impact", 0) >= bar]
            eq = stt.get("paper_equity"); mret = stt.get("monthly_return")
            vel = self.core.velocity(self.learner.lgate) if getattr(self.learner, "lgate", None) else {}
            spd = vel.get("samples_per_day")
            b7 = vel.get("brier_7d")
            btrend = ("improving" if vel.get("learning") else
                      "FLAT — no edge yet" if vel.get("no_edge") else "settling")
            # THE headline Brady asked for: is it on the right path to eventually trade well?
            m7 = vel.get("mastery_7d")
            mtrend = f" ({m7:+.1f} this week)" if isinstance(m7, (int, float)) else ""
            vmap = {
                "ON TRACK": f"🧠 GETTING SMARTER? ✅ ON TRACK — mastery {vel.get('mastery_now', 0)}{mtrend} "
                            "is measurably climbing / the practice trades are beating the market. Leave it running.",
                "SPINNING": f"🧠 GETTING SMARTER? ⚠️ SPINNING — lots of activity, but mastery {vel.get('mastery_now', 0)}"
                            f"{mtrend} isn't climbing and the practice trades aren't beating the market. This setup "
                            "may be topping out below where you need it.",
                "TOO EARLY": "🧠 GETTING SMARTER? ⏳ TOO EARLY — still gathering enough resolved trades/days to "
                             "judge the trend honestly. Give it ~1–2 more weeks of this.",
                "SETTLING": f"🧠 GETTING SMARTER? ➖ SETTLING — mastery {vel.get('mastery_now', 0)}{mtrend}, "
                            "no clear up-trend yet. Watch the next few days.",
            }
            self.approvals.daily_digest(
                vmap.get(vel.get("verdict", "TOO EARLY"), "") + "\n\n"
                "Still in practice mode — no real money. Daily heartbeat:\n"
                f"• Son's shadow equity: ${eq} (started $500)" + (f", ~{mret:+.1f}%/mo\n" if isinstance(mret, (int, float)) else "\n") +
                f"• Mastery: {stt.get('mastery', rep['mastery'])}/{stt.get('needs_mastery', '?')} to unlock the next rung "
                f"({stt.get('samples', '?')} resolved trades)\n"
                f"• Learning velocity: {vel.get('n', '?')} P(win) samples"
                f"{f' (+{spd}/day)' if spd is not None else ''}, calibration Brier {vel.get('brier', '?')} "
                f"({btrend}{f', 7d {b7:+.3f}' if b7 is not None else ''}); "
                f"{vel.get('explore_trades', 0)} practice trades, dir-hit "
                f"{round(100*vel['dir_hit']) if vel.get('dir_hit') is not None else '?'}%.\n"
                f"• Today I retuned {syn['weights_moved']} source trust-levels and wrote {len(lessons)} new lesson(s).\n"
                f"• Self-improvement ideas: considered {len(pool)}, {len(passed)} clear my confidence bar, "
                f"{len(unsent)} waiting for your ✅/❌.\n"
                + ("• ⚠️ HONEST: thousands of samples in and calibration still isn't improving — I have found "
                   "NO real edge in this configuration yet.\n" if vel.get("no_edge") else "")
                + f"• Honest edge check: {honesty.get('status', 'still gathering data')[:130]}")
        return {"syn": syn, "rules": rules, "studies": studies, "calib": cal, "redteam": rt,
                "benchmark": bench, "lessons": lessons, "code_proposals": code_props,
                "new_sources": new_sources, "by": "groq+brain" if v else "brain"}

    def _log(self, rep, syn, rules, studies, cal, rt, bench, lessons, code_props, new_sources, by):
        os.makedirs(EVO_DIR, exist_ok=True)
        if not os.path.exists(LOG_MD):
            with open(LOG_MD, "w") as f:
                f.write("# TENTACLE — Evolution Log\n\nHow the son grew, 24/7, with his whole brain wired "
                        "together. Weights/rules/lessons auto-applied (params only); code changes are "
                        "PROPOSALS in `code_proposals.jsonl` for Brady to review.\n")
        sim = rep["sim"]
        block = [f"\n## {iso(time.time())}  ·  by {by}",
                 f"- mood: {rep['mood']}",
                 f"- son: mastery {rep['mastery']}, growth {rep['growth_progress']*100:.0f}%; "
                 f"sims {sim['episodes']} (win {sim['win_rate']}), sim-equity ${sim['sim_equity']} ({sim['grown_pct']:+.1f}%)",
                 f"- BRAIN FUSION: moved {syn['weights_moved']} source weights, {syn['decayed_patterns']} decayed patterns retired",
                 f"- learned trading rules: stop {rules.get('stop_pct','?')}% / target {rules.get('target_pct','?')}% / "
                 f"hold {rules.get('hold_days','?')}d (decade win {rules.get('win','?')})",
                 f"- catalyst studies: gap-and-go win {studies.get('gap_win','?')} (n{studies.get('gap_n',0)}), "
                 f"breakout win {studies.get('breakout_win','?')} (n{studies.get('breakout_n',0)})",
                 f"- calibration factor: x{cal.get('factor',1.0)} ({cal.get('n',0)} outcomes)",
                 f"- safety red-team: {'ALL GATES HOLD' if rt['ok'] else '⚠ FAILED: ' + str(rt['failed'])}",
                 f"- vs SPY buy-and-hold: edge/trade {bench.get('edge_per_trade_pct','n/a')}% "
                 f"(sim {bench.get('sim_avg_pct','?')}% vs SPY {bench.get('spy_avg_hold_pct','?')}% per hold)",
                 f"- strategy tournament (top by walk-forward expectancy): "
                 + ", ".join(f"{r['style']} {r['rule']} oos{r['oos_u']} ({r['folds_pos']}/{r['folds']} folds+)"
                             for r in rep.get('strategy_leaderboard', [])[:3]),
                 f"- HONESTY (overfit check): {rep.get('honesty_out_of_sample', {}).get('status', 'n/a')}",
                 f"- ambition: {rep.get('ambition', {}).get('note', '')}",
                 f"- new data-source ideas: {', '.join(new_sources) if new_sources else 'none'}",
                 f"- lessons: {'; '.join(lessons) if lessons else 'none'}",
                 f"- code proposals (await review): {len(code_props)}"]
        with open(LOG_MD, "a") as f:
            f.write("\n".join(block) + "\n")
