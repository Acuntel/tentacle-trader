"""The brain: classify the catalyst, score conviction across independent tentacles, size the bet."""
from __future__ import annotations

import math
from collections import defaultdict

from .models import RawSignal, Candidate, Catalyst
from . import prices
from . import technicals
from . import options
from . import fundamentals
from . import dossier

RAW_SCALE = 2.0  # raw≈2 -> ~63, raw≈3 -> ~78, raw≈4 -> ~86

# Signal "families" — convergence ACROSS families (filing + surprise + flow) is where edge lives,
# not two of the same kind (two Reddit posts). The brain rewards cross-family agreement.
FAMILY = {
    "edgar_8k": "filing", "edgar_form4": "filing", "edgar_13d": "filing", "newswire": "filing",
    "sec_fulltext": "filing", "hiring": "alt", "hiring_ats": "alt",
    "appstore": "demand", "steam": "demand", "github": "demand",
    "pypi": "demand", "npm": "demand", "docker": "demand", "appreviews": "demand",
    "web_traffic": "demand", "onchain": "onchain", "jets": "flow", "congress": "flow",
    "finnhub_earnings": "surprise",
    "alpaca_news": "news", "finnhub": "news", "google_news": "news", "hackernews": "news",
    "fda": "regulatory", "gov_contract": "regulatory", "index_inclusion": "regulatory",
    "nasdaq_halts": "regulatory",
    "polymarket": "crowd",
    "wiki_attention": "attention",
    "x_relay": "flow",
    "apewisdom": "social", "stocktwits": "social", "reddit": "social",
    "discord": "social", "telegram": "social",
}

KEYWORDS = {
    Catalyst.EARNINGS_BEAT: ("beats", "tops estimate", "raises guidance", "record revenue",
                             "raises full-year", "earnings beat", "smashes"),
    Catalyst.INDEX_INCLUSION: ("s&p 500", "added to the s&p", "join the s&p", "index inclusion",
                               "nasdaq-100", "will be added"),
    Catalyst.FDA_APPROVAL: ("fda approv", "pdufa", "phase 3", "fda clears", "marketing authorization",
                            "breakthrough therapy"),
    Catalyst.MNA_TARGET: ("to acquire", "to be acquired", "acquisition of", "merger", "takeover",
                          "buyout", "agrees to buy", "in talks to acquire"),
    Catalyst.GOV_CONTRACT: ("awarded", "contract worth", "defense contract", "pentagon", "nasa awards",
                            "government contract"),
    Catalyst.ACTIVIST_STAKE: ("activist", "13d", "builds stake", "takes stake", "elliott", "icahn", "starboard"),
}


def classify(sig: RawSignal) -> Catalyst:
    if sig.catalyst_hint:
        try:
            return Catalyst(sig.catalyst_hint)
        except ValueError:
            pass
    low = (sig.headline or "").lower()
    for cat, kws in KEYWORDS.items():
        if any(k in low for k in kws):
            return cat
    return Catalyst.UNKNOWN


class Brain:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.conv = cfg["conviction"]
        self.cats = cfg["catalysts"]
        self.patterns = None        # PatternMemory, attached by main
        self.analyst = None         # Analyst (reasoning judge), attached by main
        self.lgate = None           # LearnedGate (P(win) model), attached by main
        self.hist_gate = None       # decade-trained historical alpha prior (transfer-gap), attached by main
        self.core = None            # shared BrainCore (learned rules + calibration), attached by main
        self.macro = ""             # current macro regime tag, set by main each cycle
        # In SHADOW we EXPLORE: take every setup that clears hard safety so the learner gets real,
        # labeled out-of-sample outcomes (the only way mastery/the P(win) model can ever grow).
        # In LIVE we EXPLOIT: all soft/heuristic filters are enforced. Real money is separately
        # gated by the Gauntlet + manual arm in executor.py — exploring here risks nothing.
        self.mode = cfg.get("identity", {}).get("mode", "shadow")
        self.explore = (self.mode == "shadow") and bool(self.conv.get("shadow_explore", True))
        self.tech = cfg.get("technicals", {})           # candlestick / indicator entry-timing layer
        self.tech_on = bool(self.tech.get("enabled", True))
        self.opt = cfg.get("options", {})               # options/IV positioning layer
        self.opt_on = bool(self.opt.get("enabled", True))
        self.fund = cfg.get("fundamentals", {})          # float / short-interest / quality layer
        self.fund_on = bool(self.fund.get("enabled", True))

    def _score(self, sources_strength: dict[str, float], prior: float,
               earliness: float, n_families: int) -> float:
        # raw = sum of (weight * strength) across DISTINCT sources (independence rewarded)
        raw = sum(sources_strength.values())
        base = 100 * (1 - math.exp(-raw / RAW_SCALE))
        # convergence: agreement across DIFFERENT signal families is the real edge
        fbonus = float(self.conv.get("family_bonus", 0.18))
        convergence = min(1.5, 1 + fbonus * max(0, n_families - 1))
        return max(0.0, min(100.0, base * prior * earliness * convergence))

    def _size(self, score: float, capital: float, pwin: float | None = None) -> float:
        frac = 0.0
        for band in self.conv["size_bands"]:
            if score >= band["min"]:
                frac = band["frac"]
                break
        # EDGE-BASED sizing: when the P(win) model is trained AND well-calibrated, lean the size on its
        # edge above a coin-flip (Kelly-lite), blended with the score bands by how much we TRUST it.
        if pwin is not None and self.lgate is not None and self.lgate.ready():
            edge = max(0.0, min(1.0, (pwin - 0.5) * 4.0))   # 0 at P(win)=0.5, 1.0 at P(win)>=0.75
            trust = self.lgate.trust()                      # 0 if poorly calibrated, 1 if sharp (Brier)
            frac *= (1 - trust) + trust * (0.5 + edge)
        return round(capital * frac, 2)

    def evaluate(self, signals: list[RawSignal], capital: float, regime_down: bool) -> list[Candidate]:
        by_ticker: dict[str, list[RawSignal]] = defaultdict(list)
        for s in signals:
            for tk in s.tickers:
                by_ticker[tk].append(s)

        out: list[Candidate] = []
        for tk, sigs in by_ticker.items():
            # best strength per distinct source (don't let one source double-count)
            per_source: dict[str, float] = {}
            for s in sigs:
                per_source[s.source] = max(per_source.get(s.source, 0.0), s.weight * s.strength)

            # dominant catalyst among the signals
            votes: dict[Catalyst, float] = defaultdict(float)
            for s in sigs:
                votes[classify(s)] += s.weight * s.strength
            catalyst = max(votes, key=votes.get)
            if catalyst == Catalyst.UNKNOWN and len(votes) > 1:
                votes.pop(Catalyst.UNKNOWN, None)
                catalyst = max(votes, key=votes.get)

            ccfg = self.cats.get(catalyst.value, {"prior": 1.0, "hold_days": 5, "stop_pct": 8, "target_pct": 12})
            if self.core is not None:                 # use the decade-optimized trading rules when learned
                ccfg = self.core.rule(catalyst.value, ccfg)

            # only spend a yfinance price call on candidates that could ACTUALLY qualify
            # (>= min sources AND min families) — at 10k tentacles this prevents a Yahoo ban
            families = {FAMILY.get(s, "other") for s in per_source}
            qualifies = (len(per_source) >= self.conv["min_independent_sources"]
                         and len(families) >= self.conv.get("min_families", 1))
            runup = prices.runup_pct_today(tk) if qualifies else None
            earliness = 0.9 if runup is None else (0.5 if runup > self.conv["max_runup_pct"] else 1.0)
            score = self._score(per_source, ccfg["prior"], earliness, len(families))

            # macro-regime tilt (rate cuts / risk-on help; risk-off hurts)
            if "risk_off" in self.macro:
                score *= 0.9
            elif "risk_on" in self.macro:
                score *= 1.05
            # learned PATTERN recognition: amplify proven setups, set data-driven profit target
            target, sig = ccfg["target_pct"], ""
            if self.patterns is not None:
                sig = self.patterns.signature(catalyst.value, families, self.macro)
                score *= self.patterns.multiplier(sig)
                target = self.patterns.suggested_target(sig, ccfg["target_pct"])

            # PRICE ACTION (candlesticks + RSI/trend) decides the TIMING — only for names that already
            # cleared the catalyst gate, so we never price-scan the whole fleet (anti-ban).
            tech_notes, tech_veto = [], None
            opt_notes, opt_confirm = [], False
            if qualifies and self.tech_on:
                allow, tmult, tech_notes = technicals.entry_read(tk, self.tech)
                score *= tmult
                if not allow:
                    tech_veto = "price action says wait — bearish reversal at the highs"
            if qualifies and self.opt_on:
                omult, opt_confirm, opt_notes = options.entry_read(tk, self.opt)
                score *= omult                              # options agreement strengthens, fear weakens
            fund_notes, feats = [], {}
            if qualifies and self.fund_on:
                fmult, fund_notes = fundamentals.entry_read(tk, self.fund)
                score *= fmult                              # squeeze fuel boosts, zombie quality shrinks
                feats = fundamentals.features(tk)
            if self.core is not None:                  # calibration: scale conviction to realized win-rate
                score *= self.core.calib_factor(score)
            score = max(0.0, min(100.0, score))

            eff_families = len(families) + (1 if opt_confirm else 0)
            reasons = [
                f"{len(per_source)} sources / {eff_families} families "
                f"({', '.join(sorted(families))}{', options' if opt_confirm else ''}): "
                f"{', '.join(sorted(per_source))}",
                f"catalyst={catalyst.value}, macro={self.macro or 'n/a'}, pattern={sig or 'new'}",
                f"take-profit {target}% / stop {ccfg['stop_pct']}%",
            ]
            if tech_notes:
                reasons.append("price action: " + " | ".join(tech_notes))
            if opt_notes:
                reasons.append("options: " + " | ".join(opt_notes))
            if fund_notes:
                reasons.append("fundamentals: " + " | ".join(fund_notes))
            cand = Candidate(
                ticker=tk, catalyst=catalyst, confidence=score,
                size_usd=self._size(score, capital), signals=sigs, runup_pct=runup,
                regime_ok=not regime_down,
                stop_pct=ccfg["stop_pct"], target_pct=target, hold_days=ccfg["hold_days"],
                reasons=reasons,
            )
            cand._families = eff_families
            cand._signature = sig
            cand._tech_veto = tech_veto
            cand._options_confirm = opt_confirm
            cand._features = feats                          # fundamentals features for the learned model
            cand._qualified = qualifies                     # WS1: cleared sources+families -> has a price + features

            # ANALYST BRAIN — reason over a real dossier (independence + judge + learned P(win)),
            # not source-counting. Runs only on already-qualifying names (cheap, LLM-gated).
            if qualifies and self.analyst is not None:
                if self.lgate is not None and getattr(self, "distill", None) is None:
                    from .distill import DistilledJudge      # learn to mimic the LLM; share with the analyst
                    self.distill = DistilledJudge(self.lgate.know)
                    self.analyst.distill = self.distill
                dos = dossier.build(cand, self.macro)
                verdict = self.analyst.judge(dos)
                cand._verdict = verdict
                cand.confidence = max(0.0, min(100.0, cand.confidence * (0.6 + 0.008 * verdict["conviction"])))
                # ORTHOGONALITY: real confirmation is agreement across INDEPENDENT evidence TYPES (the
                # dossier's deduped pillars), not echoed sources. Reward 3+ orthogonal pillars; discount
                # a lone-pillar setup that only looked confirmed because two sources echoed one wire.
                indep = dos.get("independence", 1)
                cand.confidence = min(100.0, cand.confidence * (1.08 if indep >= 3 else 1.0 if indep == 2 else 0.92))
                if (not verdict.get("real", True)) or verdict.get("direction") != "up":
                    cand._analyst_veto = "analyst: " + (verdict.get("bear_case") or "not a clean up-catalyst")
                if self.lgate is not None:
                    cand._lgate_features = self.lgate.featurize(dos, verdict)
                    p_live = self.lgate.predict(cand._lgate_features)
                    # TRANSFER GAP: blend in the decade-trained historical ALPHA prior at a low weight,
                    # so the whole decade informs the call while the live model still owns it.
                    if self.hist_gate is not None and self.hist_gate.ready(50):
                        p_live = 0.8 * p_live + 0.2 * self.hist_gate.predict(cand._lgate_features)
                    # FORGE -> GATE: blend the free search's PROVEN policy at a low, guarded weight (only
                    # once the Forge has a real out-of-sample edge). The live model still owns the call.
                    from .forge import champion_pwin
                    fp = champion_pwin(self.lgate.know, cand._lgate_features)
                    if fp is not None:
                        p_live = 0.85 * p_live + 0.15 * fp
                    cand._pwin = round(p_live, 3)
                    cand._uncertainty = round(1 - abs(cand._pwin - 0.5) * 2, 3)   # 1.0 = most informative
                # SEMANTIC MEMORY: what happened in setups SIMILAR to this one (k-NN over bge-m3)? Bounded,
                # advisory nudge so a rare signature borrows experience from its neighbors. Fails open.
                try:
                    if getattr(self, "memory", None) is None:
                        from .memory import SemanticMemory
                        self.memory = SemanticMemory()
                    mem = self.memory.recall(getattr(cand, "_signature", ""))
                    if mem and mem["n"] >= 8:
                        cand._memory = mem
                        edge = max(-1.0, min(1.0, (mem["win_rate"] - 0.5) * 2))
                        cand.confidence = max(0.0, min(100.0, cand.confidence * (1.0 + 0.12 * edge)))
                except Exception:
                    pass
                cand.size_usd = self._size(cand.confidence, capital, pwin=getattr(cand, "_pwin", None))
                cand.reasons.append(
                    f"analyst[{verdict.get('by')}]: real={verdict['real']} dir={verdict['direction']} "
                    f"conv={verdict['conviction']} P(win)={getattr(cand, '_pwin', 'n/a')} "
                    f"| independence {dos['independence']} pillars ({', '.join(dos['pillars'])}) "
                    f"| {verdict.get('rationale', '')[:80]}")
            out.append(cand)
        # ACTIVE LEARNING: in shadow, nudge high-UNCERTAINTY (most informative) setups up the queue so
        # the learner gathers the labels that actually sharpen the model — bounded (max +6) so it never
        # overrides a clearly-strong idea. In live it's pure exploit (rank by confidence only).
        if self.explore:
            out.sort(key=lambda c: c.confidence + 6.0 * getattr(c, "_uncertainty", 0.0), reverse=True)
        else:
            out.sort(key=lambda c: c.confidence, reverse=True)
        return out

    def learning_picks(self, cands: list[Candidate], k: int, short_pwin: float,
                       allow_short: bool) -> list[tuple[Candidate, int]]:
        """WS1 (shadow only): pick the most-UNCERTAIN qualifying names to open as off-book learning
        trades — ignoring the conviction bar, min-families, regime, and capital. Each is the maximally
        informative label to gather. Still respects the HARD data-backed vetoes (don't pour samples into
        known-broken setups). Direction follows the model's own view. Returns [(candidate, direction)]."""
        if not self.explore:
            return []
        elig = []
        for c in cands:
            if not getattr(c, "_qualified", False) or getattr(c, "_lgate_features", None) is None:
                continue
            sig = getattr(c, "_signature", "")
            if self.patterns is not None and (self.patterns.is_loser(sig) or self.patterns.is_decayed(sig)):
                continue
            elig.append(c)
        # most informative first: uncertainty closest to a coin-flip teaches the most
        elig.sort(key=lambda c: getattr(c, "_uncertainty", 0.5), reverse=True)
        # WS2 DIVERSITY: cap picks per catalyst so the budget spreads across setup types — every pattern
        # reaches statistical significance, instead of 20 samples of the loudest catalyst.
        max_per_cat = int(self.cfg.get("exploration", {}).get("max_per_catalyst", 4))
        picks, per_cat = [], {}
        for c in elig:
            if len(picks) >= max(0, k):
                break
            cat = c.catalyst.value
            if per_cat.get(cat, 0) >= max_per_cat:
                continue
            pw = getattr(c, "_pwin", None)
            verdict = getattr(c, "_verdict", None)
            bearish = (pw is not None and pw < short_pwin) or (
                verdict is not None and verdict.get("direction") != "up")
            picks.append((c, -1 if (allow_short and bearish) else 1))
            per_cat[cat] = per_cat.get(cat, 0) + 1
        return picks

    def passes_gate(self, c: Candidate) -> tuple[bool, str]:
        # ---- HARD safety: enforced ALWAYS, even in shadow exploration ----
        # These are data-backed (loser/decay) or structural (it isn't even a real setup otherwise).
        if self.patterns is not None and self.patterns.is_loser(getattr(c, "_signature", "")):
            return False, "pattern recognized as a persistent loser — vetoed"
        if self.patterns is not None and self.patterns.is_decayed(getattr(c, "_signature", "")):
            return False, "pattern decayed (its recent edge collapsed — regime shift) — vetoed"
        if c.n_independent < self.conv["min_independent_sources"]:
            return False, f"only {c.n_independent} source(s); need {self.conv['min_independent_sources']}"
        fams = getattr(c, "_families", 1)
        if fams < self.conv.get("min_families", 1):
            return False, f"only {fams} signal family; need {self.conv['min_families']} (combine signal types)"
        if c.confidence < self.conv["min_score_to_trade"]:
            return False, f"score {c.confidence:.0f} < {self.conv['min_score_to_trade']}"

        # ---- SOFT filters: heuristic / not-yet-proven. In shadow we EXPLORE through them so the
        #      learner gathers the labeled outcomes that prove (or kill) them. In live they bite. ----
        if self.explore:
            return True, "shadow-explore: cleared hard safety (soft filters logged, not enforced)"

        # ---- SOFT in live only: don't buy into a bearish reversal at the highs (timing). In shadow we
        #      explore through it so it actually trades + LEARNS whether that timing loses. ----
        if getattr(c, "_tech_veto", None):
            return False, c._tech_veto
        if getattr(c, "_analyst_veto", None):
            return False, c._analyst_veto
        if (self.lgate is not None and self.lgate.ready() and getattr(c, "_pwin", None) is not None
                and c._pwin < self.conv.get("min_pwin", 0.5)):
            return False, f"learned P(win) {c._pwin:.2f} < {self.conv.get('min_pwin', 0.5)} (model says low edge)"
        if not c.regime_ok:
            # A falling broad tape doesn't go fully dark for a catalyst/momentum long book —
            # it demands MORE conviction. Only the strongest idiosyncratic catalysts fire.
            bar = self.conv["min_score_to_trade"] + self.conv.get("regime_downtrend_premium", 15)
            if c.confidence < bar:
                return False, (f"downtrend tape: score {c.confidence:.0f} < {bar:.0f} "
                               "(raised the bar in a falling market)")
        return True, "passes conviction gate"
