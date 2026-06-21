"""The night coach — the mother studying how to make her son grow, 24/7.

It COMBINES two voices (not a fallback): hard statistics (source precision, pattern win-rates, the
decade of sims) AND a free LLM (Groq) reasoning over those stats + tonight's news + the mother's
creed. From the fusion it:
  • AUTO-APPLIES bounded self-tuning live — nudges source weights and writes lessons/notes
    (the same safe, reversible mechanism the bot already uses; params only).
  • PROPOSES code changes (never applies them) — writes them to an evolution journal Brady reviews
    and merges. Nothing the bot writes as code runs until a human reads it.

So when Brady comes back he opens state/evolution/EVOLUTION_LOG.md and sees exactly how his son
grew, what the mother tuned, and what code she wants to evolve into next.
"""
from __future__ import annotations

import json
import os
import time

from . import llm
from .learning import WMIN, WMAX
from .models import iso

EVO_DIR = "state/evolution"
LOG_MD = os.path.join(EVO_DIR, "EVOLUTION_LOG.md")
PROPOSALS = os.path.join(EVO_DIR, "code_proposals.jsonl")

SYSTEM = ("You are a mother raising your SON — your trading capital. Two drives guide every choice: "
          "grow him as LARGE as he can possibly be (train him to perform at his best), and NEVER let "
          "him die (a loss hurts far more than an equal gain). You are reviewing your son's progress, "
          "a decade of historical simulations, and tonight's market news to decide how to improve. "
          "You MAY retune which data sources to trust and note lessons. You may PROPOSE code changes "
          "for a human to review, but you cannot run code yourself, and you can NEVER weaken the hard "
          "risk caps, the real-money gate, or the Gauntlet. Reply ONLY as a JSON object.")


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Coach:
    def __init__(self, cfg: dict, learner, instinct, sim, scout=None):
        c = cfg.get("coach", {})
        self.on = bool(c.get("enabled", True))
        self.every = int(c.get("every_minutes", 30)) * 60       # cadence (still runs 24/7)
        self.min_src_samples = int(c.get("min_source_samples", 8))
        self.learner = learner
        self.instinct = instinct
        self.sim = sim
        self.scout = scout                                      # finds new data sources while Brady's away
        self.last = 0.0
        self.know = learner.know
        self.know.setdefault("lessons", [])
        self.know.setdefault("coach_notes", {})
        self.last_summary = "coach has not run yet"

    def due(self) -> bool:
        return self.on and (time.time() - self.last) >= self.every

    # ---- the self-report both voices reason over ----
    def _self_report(self, news_signals, macro_ctx) -> dict:
        srcs = {}
        for s, st in self.know.get("sources", {}).items():
            if st.get("trades", 0) >= self.min_src_samples:
                srcs[s] = {"precision": round(st["wins"] / st["trades"], 3),
                           "trades": st["trades"], "weight": self.learner.weights.get(s)}
        prep = self.learner.patterns.report()
        heads = []
        seen = set()
        for sg in reversed(news_signals or []):
            h = getattr(sg, "headline", "")
            if h and h not in seen:
                seen.add(h); heads.append(h)
            if len(heads) >= 12:
                break
        dungeon = []
        try:
            with open("state/dungeon/post_mortems.jsonl") as f:
                dungeon = [json.loads(x)["root_cause"] for x in f.readlines()[-5:]]
        except Exception:
            pass
        return {
            "creed": self.instinct.creed(), "mood": self.instinct.mood(),
            "mastery": self.instinct.mastery(), "growth_progress": self.instinct.growth_progress(),
            "sim": self.sim.report(), "sim_patterns": self.sim.findings(),
            "source_precision": srcs, "pattern_best": prep.get("best", [])[:6],
            "pattern_worst": prep.get("worst", [])[:6], "macro_regime": macro_ctx,
            "recent_losses": dungeon, "tonight_news": heads,
        }

    # ---- combination: stats-driven tuning + LLM reasoning, fused ----
    def run(self, news_signals=None, macro_ctx="") -> dict:
        self.last = time.time()
        rep = self._self_report(news_signals, macro_ctx)
        tuned = []

        # VOICE 1 (always) — data-driven: pull each source's weight toward its proven precision.
        for s, info in rep["source_precision"].items():
            base = self.learner.weights.get(s, 0.5)
            target = base * (0.6 + info["precision"])           # precision 0->0.6x .. 1.0->1.6x
            new = round(max(WMIN, min(WMAX, 0.9 * base + 0.1 * target)), 3)
            if abs(new - base) >= 0.005:
                self.learner.weights[s] = new
                tuned.append(f"{s} {base}->{new} (prec {info['precision']})")

        # VOICE 2 (combined in) — the free LLM coach reasons over the report + creed.
        lessons, code_props, watch = [], [], []
        v = llm.ask_json(SYSTEM,
                         "Review your son and propose improvements. Reply as JSON with keys: "
                         'reweight (object src->"up"|"down"), lessons (array of short strings), '
                         "watchlist (array of tickers), code_proposals (array of {file, change, why}). "
                         "Base it on the evidence:\n" + json.dumps(rep, default=str)[:9000],
                         max_tokens=1200)
        if v:
            for s, dirn in (v.get("reweight") or {}).items():
                if s in self.learner.weights:
                    f = 1.06 if str(dirn).lower().startswith("up") else 0.94
                    nw = round(max(WMIN, min(WMAX, self.learner.weights[s] * f)), 3)
                    if nw != self.learner.weights[s]:
                        tuned.append(f"{s}->{nw} (coach:{dirn})")
                        self.learner.weights[s] = nw
            lessons = [str(x) for x in (v.get("lessons") or [])][:6]
            watch = [str(x) for x in (v.get("watchlist") or [])][:20]
            code_props = [p for p in (v.get("code_proposals") or []) if isinstance(p, dict)][:6]

        # APPLY the safe stuff (params only); PROPOSE the code stuff (review only).
        for ls in lessons:
            if ls not in self.know["lessons"]:
                self.know["lessons"].append(ls)
        self.know["lessons"] = self.know["lessons"][-100:]
        if watch:
            self.know["coach_notes"]["watchlist"] = watch
        for p in code_props:
            _append(PROPOSALS, {"ts": iso(time.time()), "status": "PROPOSED — review & merge",
                                "file": p.get("file"), "change": p.get("change"), "why": p.get("why")})

        # FIND NEW DATA SOURCES while Brady's away — the Scout proposes, we journal for review.
        new_sources = []
        if self.scout is not None:
            try:
                new_sources = [s for s, _ in self.scout.propose("smart_money")][:5]
            except Exception:
                new_sources = []
            for name in new_sources:
                _append(PROPOSALS, {"ts": iso(time.time()), "status": "NEW SOURCE — consider wiring",
                                    "file": "earshot/sources/", "change": f"add/enable tentacle '{name}'",
                                    "why": "scout: current sources decaying; this one could sharpen the edge"})
        self.learner._persist_know()

        self._write_log(rep, tuned, lessons, code_props, new_sources, by="groq+stats" if v else "stats")
        self.last_summary = (f"tuned {len(tuned)} weights, +{len(lessons)} lessons, "
                             f"{len(code_props)} code proposals, {len(new_sources)} new-source ideas "
                             f"(all review-only) — sim grown {rep['sim']['grown_pct']:+.1f}%")
        return {"tuned": tuned, "lessons": lessons, "code_proposals": code_props,
                "new_sources": new_sources, "by": "groq+stats" if v else "stats", "sim": rep["sim"]}

    def _write_log(self, rep, tuned, lessons, code_props, new_sources, by):
        os.makedirs(EVO_DIR, exist_ok=True)
        if not os.path.exists(LOG_MD):
            with open(LOG_MD, "w") as f:
                f.write("# TENTACLE — Evolution Log\n\n"
                        "How the son grew and the mother improved him, 24/7. "
                        "Weight/lesson tuning is auto-applied (params only); code changes are "
                        "PROPOSALS in `code_proposals.jsonl` for Brady to review & merge.\n")
        sim = rep["sim"]
        block = [f"\n## {iso(time.time())}  ·  by {by}",
                 f"- mood: {rep['mood']}",
                 f"- son: mastery {rep['mastery']}, growth {rep['growth_progress']*100:.0f}% toward goal; "
                 f"sims {sim['episodes']} (win {sim['win_rate']}, avg {sim['avg_ret']}%), "
                 f"sim-equity ${sim['sim_equity']} ({sim['grown_pct']:+.1f}%)",
                 f"- auto-tuned weights: {', '.join(tuned) if tuned else 'none'}",
                 f"- lessons learned: {'; '.join(lessons) if lessons else 'none'}",
                 f"- new data-source ideas (await wiring): {', '.join(new_sources) if new_sources else 'none'}",
                 f"- code proposals (await review): {len(code_props)}"]
        for p in code_props:
            block.append(f"    - `{p.get('file')}`: {p.get('change')}  — {p.get('why')}")
        with open(LOG_MD, "a") as f:
            f.write("\n".join(block) + "\n")
