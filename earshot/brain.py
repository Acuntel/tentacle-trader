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
    "edgar_8k": "filing", "edgar_form4": "filing", "edgar_13d": "filing",
    "sec_fulltext": "filing", "hiring": "alt", "hiring_ats": "alt",
    "appstore": "demand", "steam": "demand", "github": "demand",
    "pypi": "demand", "npm": "demand", "docker": "demand", "appreviews": "demand",
    "web_traffic": "demand", "onchain": "onchain", "jets": "flow", "congress": "flow",
    "finnhub_earnings": "surprise",
    # W1.7: "newswire" = the PR wires (GlobeNewswire/PRNewswire/BusinessWire) that Google/Yahoo
    # REPUBLISH — news-like, NOT a filing. As "filing" a press release + its own news echo counted
    # as 2 independent families (fake convergence) and escaped the dossier's news dedup.
    "newswire": "news",
    "alpaca_news": "news", "finnhub": "news", "google_news": "news", "hackernews": "news",
    # AUTOPSY FIX 2026-09-01: "fda" is ClinicalTrials.gov trial-STATUS rows (a study finishing
    # enrollment), not an approval. As a "regulatory" TRIGGER it manufactured the book's biggest
    # catalyst (38 trades incl. LLY at $450) out of non-events. Confirm-only now; the real
    # approval event is "fda_approvals" (openFDA Drugs@FDA).
    "fda": "news", "fda_approvals": "regulatory", "gov_contract": "regulatory",
    "index_inclusion": "regulatory", "nasdaq_halts": "regulatory", "pdufa": "regulatory",
    "polymarket": "crowd",
    "wiki_attention": "attention",
    "x_relay": "flow",
    "apewisdom": "social", "stocktwits": "social", "reddit": "social",
    "discord": "social", "telegram": "social",
}

# EDGE_MASTERPROMPT family doctrine — edge = information x (1 - competition).
# TRIGGER families carry slow-diffusion information whose half-life (days-weeks) dwarfs our
# ~30-min latency; a conviction setup MUST contain at least one. CONFIRM families (news,
# attention, onchain, other) corroborate but can never anchor a setup — fast news alone is a
# race we structurally lose. ANTI families measure COMPETITION: they never count toward
# confirmation and their presence penalizes the score (every learner-vetoed pattern contains
# +social — when the crowd confirms, we're late).
TRIGGER_FAMILIES = {"filing", "regulatory", "surprise", "demand", "flow", "alt"}
ANTI_FAMILIES = {"social", "crowd"}

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

    def _size(self, score: float, capital: float, pwin: float | None = None,
              catalyst: str | None = None) -> float:
        frac = 0.0
        for band in self.conv["size_bands"]:
            if score >= band["min"]:
                frac = band["frac"]
                break
        # EDGE-BASED sizing: when the P(win) model is trained AND well-calibrated, lean the size on its
        # edge above THE FIELD (Kelly-lite), blended with the score bands by how much we TRUST it.
        # AUDIT FIX: the edge was anchored at 0.5, but P(win) is calibrated on alpha labels whose base
        # rate is ~0.34 — the model's median prediction sits near 0.25, so edge was 0 for ~97% of setups
        # and every size got a flat 0.70x haircut. Anchor at the live field rate; at-field = neutral 1.0.
        if pwin is not None and self.lgate is not None and self.lgate.ready():
            from .patterns import field_rate
            fld = field_rate(self.lgate.know)
            edge = max(-1.0, min(1.0, (pwin - fld) / max(0.15, 1 - fld) * 2.5))
            trust = self.lgate.trust()                      # 0 if poorly calibrated, 1 if sharp (Brier)
            frac *= max(0.25, (1 - trust) + trust * (1.0 + 0.5 * edge))   # at-field -> 1.0, capped floor
        # EVIDENCE-SIZED (2026-07-31): the bands may command up to 100% of capital, but big size
        # must be EARNED. A catalyst whose own real-money record is negative (or empty) caps at
        # 25% of equity until it digs itself out; $-positive/graduated catalysts keep full bands.
        # The score is self-assessed conviction — the ledger is what actually happened.
        if catalyst and self.patterns is not None and not self.patterns.catalyst_graduated(catalyst):
            if (self.patterns.grad.get(catalyst) or {}).get("usd", 0.0) <= 0:
                # AUTOPSY FIX 2026-09-02: 0.15, not 0.25. The evidence cap hardcoded the SAME 0.25
                # that max_position_frac applies two lines below, so it could never bind on any
                # input — an unproven, $-negative catalyst sized exactly like a proven one. An
                # unproven catalyst now takes a genuinely smaller position than a graduated one.
                frac = min(frac, float(self.conv.get("unproven_position_frac", 0.15)))
        # AUTOPSY FIX 2026-09-01: hard per-name cap. One saturated-score name (LLY, $450 of $500)
        # was 31% of all losses; the 31 max-confidence trades lost $71 of the $88 total.
        frac = min(frac, float(self.conv.get("max_position_frac", 0.25)))
        return round(capital * frac, 2)

    @staticmethod
    def _soft(score: float) -> float:
        """AUTOPSY FIX 2026-09-01: de-saturate. ~12 stacked multipliers pushed most qualifying names
        into the hard 100 clamp, so ranking collapsed to a tiebreak and the top size band fired on
        ties. Above 80 the score is compressed smoothly (100 -> ~87, 120 -> ~93, 200 -> ~99.5):
        monotone, so ordering is preserved, but 100 stops being the modal score."""
        s = max(0.0, float(score))
        if s > 80.0:
            s = 80.0 + 20.0 * (1.0 - math.exp(-(s - 80.0) / 40.0))
        return min(100.0, s)

    def evaluate(self, signals: list[RawSignal], capital: float, regime_down: bool) -> list[Candidate]:
        by_ticker: dict[str, list[RawSignal]] = defaultdict(list)
        for s in signals:
            for tk in s.tickers:
                by_ticker[tk].append(s)

        out: list[Candidate] = []
        # NEAR-MISS learning budget (shadow only): a bounded number of names that clear min-sources but
        # NOT min-families still get the full data workup (price/tech/options/fundamentals/analyst) so
        # they become off-book LEARNING trades. The conviction gate is untouched (min_families still
        # bites there) — this only widens what the learner can practice on. Without it the explore
        # flood starves: cross-family co-occurrence is rare, so almost nothing "qualifies" to learn from.
        nearmiss_budget = int(self.cfg.get("exploration", {}).get("nearmiss_per_cycle", 8)) \
            if self.explore else 0
        nearmiss_used = 0
        for tk, sigs in by_ticker.items():
            # best strength per distinct source (don't let one source double-count).
            # W1.8: LEAD-TIME weighting — a source that historically fires FIRST on confirmed setups
            # counts more than a follower echoing it (early is the main edge free data offers).
            # Weighting only: gate thresholds are untouched; no core/lead data -> neutral 1.0.
            per_source: dict[str, float] = {}
            lead_f: dict[str, float] = {}
            for s in sigs:
                if s.source not in lead_f:
                    lead_f[s.source] = (self.core.lead_factor(s.source)
                                        if self.core is not None else 1.0)
                per_source[s.source] = max(per_source.get(s.source, 0.0),
                                           s.weight * s.strength * lead_f[s.source])

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
            # (>= min sources AND min families) — at 10k tentacles this prevents a Yahoo ban.
            # Doctrine: ANTI families are stripped from the confirmation count, and >=1 TRIGGER
            # family is required (trigger-less multi-family names fall to the near-miss lane).
            all_families = {FAMILY.get(s, "other") for s in per_source}
            anti_n = sum(1 for s in per_source if FAMILY.get(s, "other") in ANTI_FAMILIES)
            families = all_families - ANTI_FAMILIES
            qualifies = (len(per_source) >= self.conv["min_independent_sources"]
                         and len(families) >= self.conv.get("min_families", 1)
                         and bool(families & TRIGGER_FAMILIES))
            learn_scope = qualifies
            if (not qualifies and nearmiss_used < nearmiss_budget
                    and len(per_source) >= self.conv["min_independent_sources"]):
                learn_scope = True                          # full workup for learning, not for conviction
                nearmiss_used += 1
            runup = prices.runup_pct_today(tk) if (learn_scope and tk) else None
            earliness = 0.9 if runup is None else (0.5 if runup > self.conv["max_runup_pct"] else 1.0)
            score = self._score(per_source, ccfg["prior"], earliness, len(families))

            # macro-regime tilt (rate cuts / risk-on help; risk-off hurts)
            if "risk_off" in self.macro:
                score *= 0.85
            elif "risk_on" in self.macro:
                score *= 1.05
            # crowding penalty: social/crowd presence = competition already on the trade
            if anti_n:
                score *= max(0.70, 1.0 - 0.08 * anti_n)
            # learned PATTERN recognition: amplify proven setups, set data-driven profit target.
            # Signatures keep the FULL family set (incl. social) so the learner still sees crowd
            # presence as a pattern feature — and old stored signatures stay comparable.
            target, sig = ccfg["target_pct"], ""
            if self.patterns is not None:
                sig = self.patterns.signature(catalyst.value, all_families, self.macro)
                score *= self.patterns.multiplier(sig)
                target = self.patterns.suggested_target(sig, ccfg["target_pct"])
                if self.patterns.catalyst_graduated(catalyst.value):
                    score *= 1.10               # a PROVEN forward ledger earns extra conviction
            # AUTOPSY FIX 2026-09-01: reward:risk floor. Breakeven at a 6% stop / 4% target needs a
            # 62% hit rate; the book runs ~41%. No candidate may carry a target under
            # min_reward_risk x its stop — the learned payoff can shrink the target only down to here.
            min_rr = float(self.conv.get("min_reward_risk", 1.5))
            target = max(float(target), round(min_rr * float(ccfg["stop_pct"]), 1))

            # PRICE ACTION (candlesticks + RSI/trend) decides the TIMING — only for names that already
            # cleared the catalyst gate, so we never price-scan the whole fleet (anti-ban).
            tech_notes, tech_veto = [], None
            opt_notes, opt_confirm = [], False
            if learn_scope and self.tech_on:
                allow, tmult, tech_notes = technicals.entry_read(tk, self.tech)
                score *= tmult
                if not allow:
                    tech_veto = "price action says wait — bearish reversal at the highs"
            if learn_scope and self.opt_on:
                omult, opt_confirm, opt_notes = options.entry_read(tk, self.opt)
                score *= omult                              # options agreement strengthens, fear weakens
            fund_notes, feats = [], {}
            if learn_scope and self.fund_on:
                fmult, fund_notes = fundamentals.entry_read(tk, self.fund)
                score *= fmult                              # squeeze fuel boosts, zombie quality shrinks
                feats = fundamentals.features(tk)
                # under-covered is where slow diffusion lives: a micro-cap WITH real slow
                # information gets a tilt (risk law — price/liquidity floors — untouched)
                if feats.get("micro_cap") and (families & TRIGGER_FAMILIES):
                    score *= 1.06
            # FDA PRICED-IN GAUGE (2026-08-05): prediction-market approval odds measure how much of
            # the event the market already expected — the sell-the-news failure mode of the one
            # $-positive catalyst. High odds dampen (shadow) / veto (live in passes_gate); low odds =
            # genuine surprise, the drift the ledger actually harvests. No priced market -> no-op.
            odds_note, odds_veto, fda_prob, event_ts = None, None, None, None
            if learn_scope and catalyst in (Catalyst.FDA_APPROVAL, Catalyst.PDUFA_RUNUP):
                ocfg = self.cfg.get("fda_odds", {})
                if ocfg.get("enabled", True):
                    try:
                        from . import fdaodds
                        o = fdaodds.engine().odds_for(tk)
                    except Exception:
                        o = None
                    if o:
                        fda_prob = o["prob"]
                        odds_note = f"prediction-market approval odds {fda_prob:.0%} ({o['src']})"
                        if catalyst == Catalyst.FDA_APPROVAL:
                            if fda_prob >= float(ocfg.get("priced_in_prob", 0.90)):
                                score *= 0.55
                                odds_veto = (f"approval was ~{fda_prob:.0%} expected on the prediction "
                                             "market — priced in, sell-the-news risk")
                            elif fda_prob <= float(ocfg.get("surprise_prob", 0.80)):
                                score *= 1.12
            if catalyst == Catalyst.PDUFA_RUNUP:
                # stamp the decision date -> a hard exit-by date (out pre_event_days BEFORE the event;
                # exits.py fires 'pre_event' regardless of P&L — never hold the binary coin flip)
                dts = max((float((s.raw or {}).get("decision_ts") or 0) for s in sigs), default=0.0)
                if dts > 0:
                    event_ts = dts
            if self.core is not None:                  # calibration: scale conviction to realized win-rate
                score *= self.core.calib_factor(score)
            score = self._soft(score)                  # de-saturate (see _soft): no pile-up at 100

            # LEARNING VELOCITY: emit near-miss candidates for labeling even when they won't trade
            if not qualifies and nearmiss_used < nearmiss_budget and len(per_source) >= self.conv["min_independent_sources"]:
                nearmiss_used += 1
                learn_scope = True

            eff_families = len(families) + (1 if opt_confirm else 0)
            reasons = [
                f"{len(per_source)} sources / {eff_families} families "
                f"({', '.join(sorted(families))}{', options' if opt_confirm else ''}): "
                f"{', '.join(sorted(per_source))}",
                f"catalyst={catalyst.value}, macro={self.macro or 'n/a'}, pattern={sig or 'new'}",
                f"take-profit {target}% / stop {ccfg['stop_pct']}%",
            ]
            if anti_n:
                reasons.append(f"crowding: {anti_n} social/crowd source(s) — penalized, not confirming")
            if tech_notes:
                reasons.append("price action: " + " | ".join(tech_notes))
            if opt_notes:
                reasons.append("options: " + " | ".join(opt_notes))
            if fund_notes:
                reasons.append("fundamentals: " + " | ".join(fund_notes))
            cand = Candidate(
                ticker=tk, catalyst=catalyst, confidence=score,
                size_usd=self._size(score, capital, catalyst=catalyst.value), signals=sigs, runup_pct=runup,
                regime_ok=not regime_down,
                stop_pct=ccfg["stop_pct"], target_pct=target, hold_days=ccfg["hold_days"],
                reasons=reasons,
            )
            cand._families = eff_families
            cand._signature = sig
            cand._fda_odds = fda_prob                       # priced-in gauge (None = no priced market)
            cand._odds_veto = odds_veto                     # live-only sell-the-news veto (passes_gate)
            cand._event_ts = event_ts                       # FDA decision date (pdufa_runup only)
            if event_ts:
                pre_d = float(self.cfg.get("exits", {}).get("pre_event_days", 14))
                cand._exit_by_ts = event_ts - pre_d * 86400
            if odds_note:
                cand.reasons.append(odds_note)
            cand._tech_veto = tech_veto
            cand._options_confirm = opt_confirm
            cand._features = feats                          # fundamentals features for the learned model
            cand._qualified = qualifies                     # WS1: cleared sources+families -> has a price + features
            cand._qualified_learn = learn_scope             # near-miss: explorable, but never conviction

            # ANALYST BRAIN — reason over a real dossier (independence + judge + learned P(win)),
            # not source-counting. Runs only on already-qualifying names (cheap, LLM-gated).
            if learn_scope and self.analyst is not None:
                if self.lgate is not None and getattr(self, "distill", None) is None:
                    from .distill import DistilledJudge      # learn to mimic the LLM; share with the analyst
                    self.distill = DistilledJudge(self.lgate.know)
                    self.analyst.distill = self.distill
                dos = dossier.build(cand, self.macro)
                verdict = self.analyst.judge(dos)
                cand._verdict = verdict
                cand.confidence = self._soft(cand.confidence * (0.6 + 0.008 * verdict["conviction"]))
                # ORTHOGONALITY: real confirmation is agreement across INDEPENDENT evidence TYPES (the
                # dossier's deduped pillars), not echoed sources. Reward 3+ orthogonal pillars; discount
                # a lone-pillar setup that only looked confirmed because two sources echoed one wire.
                indep = dos.get("independence", 1)
                cand.confidence = self._soft(cand.confidence * (1.08 if indep >= 3 else 1.0 if indep == 2 else 0.92))
                if (not verdict.get("real", True)) or verdict.get("direction") != "up":
                    cand._analyst_veto = "analyst: " + (verdict.get("bear_case") or "not a clean up-catalyst")
                if self.lgate is not None:
                    cand._lgate_features = self.lgate.featurize(dos, verdict)
                    # W2.1: route the prediction to the head that owns this trade's HOLD horizon.
                    # A multi-day hold (>=3d) is judged by the ~120h head once it has real samples;
                    # shorter holds (and a cold h120 head) fall back to the legacy 24h "model".
                    _gate = self.lgate
                    if ccfg.get("hold_days", 5) >= 3:
                        from .learnedgate import head as _lg_head
                        _h120 = _lg_head(self.lgate.know, "model_h120")
                        if _h120.ready(50):
                            _gate = _h120
                    p_live = _gate.predict(cand._lgate_features)
                    # TRANSFER GAP: blend in the decade-trained historical ALPHA prior at a low weight,
                    # so the whole decade informs the call while the live model still owns it.
                    # AUTOPSY FIX 2026-09-02: the decade prior may only speak once it has PROVEN
                    # skill. Live it had 9,038,391 replays at Brier 0.2519 — worse than no skill —
                    # and was still blended into every P(win) at a flat 20%, i.e. pure noise
                    # injection. trust() is 0 at or below the no-skill floor and rises with real
                    # calibration, so an unproven prior now carries exactly zero weight and a
                    # marginally-skilled one carries a proportionally small one.
                    _ht = self.hist_gate
                    if _ht is not None and _ht.ready(50):
                        _hw = 0.2 * max(0.0, min(1.0, _ht.trust()))
                        if _hw > 0:
                            p_live = (1 - _hw) * p_live + _hw * _ht.predict(cand._lgate_features)
                    # FORGE -> GATE: blend the free search's PROVEN policy at a low, guarded weight (only
                    # once the Forge has a real out-of-sample edge). The live model still owns the call.
                    from .forge import champion_pwin
                    fp = champion_pwin(self.lgate.know, cand._lgate_features)
                    if fp is not None:
                        p_live = 0.85 * p_live + 0.15 * fp
                    cand._pwin = round(p_live, 3)
                    # AUDIT FIX: informativeness anchored at the FIELD rate (predictions cluster near
                    # ~0.34, so a 0.5 anchor made "uncertainty" a monotone pwin sort, not uncertainty).
                    from .patterns import field_rate as _fr
                    _fld = _fr(self.lgate.know)
                    _half = max(_fld, 1 - _fld)
                    cand._uncertainty = round(max(0.0, 1 - abs(cand._pwin - _fld) / _half), 3)
                # SEMANTIC MEMORY: what happened in setups SIMILAR to this one (k-NN over bge-m3)? Bounded,
                # advisory nudge so a rare signature borrows experience from its neighbors. Fails open.
                try:
                    if getattr(self, "memory", None) is None:
                        from .memory import SemanticMemory
                        self.memory = SemanticMemory()
                    mem = self.memory.recall(getattr(cand, "_signature", ""))
                    if mem and mem["n"] >= 8:
                        cand._memory = mem
                        # AUDIT FIX: field-anchored (win_rate here is an alpha-label rate, base ~0.34 —
                        # the 0.5 anchor penalized even setups winning ABOVE the field)
                        from .patterns import field_rate as _fr2
                        _fld2 = _fr2(self.lgate.know) if self.lgate is not None else 0.5
                        edge = max(-1.0, min(1.0, (mem["win_rate"] - _fld2) / max(_fld2, 1 - _fld2)))
                        cand.confidence = self._soft(cand.confidence * (1.0 + 0.12 * edge))
                except Exception:
                    pass
                cand.size_usd = self._size(cand.confidence, capital, pwin=getattr(cand, "_pwin", None),
                                           catalyst=cand.catalyst.value)
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
            ok_learn = getattr(c, "_qualified_learn", getattr(c, "_qualified", False))
            if not ok_learn or getattr(c, "_lgate_features", None) is None:
                continue
            sig = getattr(c, "_signature", "")
            if self.patterns is not None and (self.patterns.is_loser(sig) or self.patterns.is_decayed(sig)):
                continue
            # a DEAD catalyst (conservatively below the field at n>=50) stops earning explore budget
            if self.patterns is not None and self.patterns.catalyst_dead(c.catalyst.value):
                continue
            elig.append(c)
        # most informative first: uncertainty closest to a coin-flip teaches the most
        elig.sort(key=lambda c: getattr(c, "_uncertainty", 0.5), reverse=True)
        # W2.3: the DIRECTIONAL head — trained on whether the side WE CHOSE beat the market
        # (learning.resolve_learning). Once it has real samples it picks the side; before that,
        # the legacy threshold/verdict heuristic decides (a 40% dir-hit was measurable, exploitable
        # signal the loop recorded but structurally could not correct).
        dhead = None
        if self.lgate is not None:
            from .learnedgate import head as _lg_head
            _dh = _lg_head(self.lgate.know, "model_dir")
            if _dh.ready(50):
                dhead = _dh
        # WS2 DIVERSITY: cap picks per catalyst so the budget spreads across setup types — every pattern
        # reaches statistical significance, instead of 20 samples of the loudest catalyst.
        # 2026-07-25: the cap is now LEDGER-DRIVEN (patterns.catalyst_explore_slots) — graduated
        # catalysts earn extra sampling, laggards get a heartbeat, `unknown` gets only a trickle.
        ecfg = self.cfg.get("exploration", {})
        max_per_cat = int(ecfg.get("max_per_catalyst", 4))
        unknown_slots = int(ecfg.get("unknown_per_cycle", 1))
        liq_floor = bool(ecfg.get("liquidity_floor", True))
        rk = self.cfg.get("risk", {})
        min_px = float(rk.get("min_price", 2.0))
        min_dv = float(rk.get("min_dollar_volume", 5e6))
        picks, per_cat = [], {}
        for c in elig:
            if len(picks) >= max(0, k):
                break
            cat = c.catalyst.value
            slots = (self.patterns.catalyst_explore_slots(cat, max_per_cat, unknown_slots)
                     if self.patterns is not None else max_per_cat)
            if per_cat.get(cat, 0) >= slots:
                continue
            # LEARN ONLY THE TRADEABLE UNIVERSE: an untradeable penny's label teaches a market we
            # can never harvest — explore's −24% avg losses lived in exactly that illiquid tail.
            # Same floors as the live risk law; both reads are TTL-cached (no extra API surface).
            if liq_floor:
                px = prices.last_price(c.ticker)
                dv = prices.dollar_volume(c.ticker)
                if (px is not None and px < min_px) or (dv is not None and dv < min_dv):
                    continue
            if dhead is not None:
                base = getattr(c, "_lgate_features", {}) or {}
                p_side = {}
                for d in (1, -1):
                    fd = dict(base)
                    fd["dir_long"] = float(d)                     # mirror learning.resolve_learning
                    fd["dirx_tech"] = d * fd.get("t_bias", 0.0)
                    fd["dirx_opt"] = d * fd.get("opt_bias", 0.0)
                    fd["dirx_down"] = d * fd.get("downtrend", 0.0)
                    p_side[d] = dhead.predict(fd)
                bearish = p_side[-1] > p_side[1]
            else:
                pw = getattr(c, "_pwin", None)
                verdict = getattr(c, "_verdict", None)
                bearish = (pw is not None and pw < short_pwin) or (
                    verdict is not None and verdict.get("direction") != "up")
            picks.append((c, -1 if (allow_short and bearish) else 1))
            per_cat[cat] = per_cat.get(cat, 0) + 1
        return picks

    def passes_gate(self, c: Candidate, enforce_soft: bool | None = None) -> tuple[bool, str]:
        """Verdict for one candidate.

        `enforce_soft` decides whether the LIVE-only filters (graduation, the vetoes, min_pwin, the
        regime bar) bite. Default = the mode's own behaviour: shadow explores through them, live
        enforces them. Passing enforce_soft=True asks the counterfactual the replay harness needs —
        "would the LIVE policy have taken this?" — without changing what shadow actually trades.
        Before this existed nothing could answer that, so ten weeks of book trades measured
        "anything with 2 sources", not the policy the money gate is supposed to protect.
        """
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
        if enforce_soft is None:
            enforce_soft = not self.explore
        if not enforce_soft:
            return True, "shadow-explore: cleared hard safety (soft filters logged, not enforced)"

        # ---- LIVE only: conviction capital flows ONLY to catalysts whose forward ledger has PROVEN
        #      alpha (graduation). Shadow explores everything so pending catalysts can earn it. ----
        if self.patterns is not None and not self.patterns.catalyst_graduated(c.catalyst.value):
            return False, (f"catalyst '{c.catalyst.value}' not graduated — needs n>="
                           f"{self.patterns.GRAD_MIN_N} forward outcomes with Wilson-lower > field")

        # ---- SOFT in live only: don't buy into a bearish reversal at the highs (timing). In shadow we
        #      explore through it so it actually trades + LEARNS whether that timing loses. ----
        if getattr(c, "_tech_veto", None):
            return False, c._tech_veto
        if getattr(c, "_analyst_veto", None):
            return False, c._analyst_veto
        if getattr(c, "_odds_veto", None):
            return False, c._odds_veto
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
