"""The brain: classify the catalyst, score conviction across independent tentacles, size the bet."""
from __future__ import annotations

import math
from collections import defaultdict

from .models import RawSignal, Candidate, Catalyst
from . import prices

RAW_SCALE = 2.0  # raw≈2 -> ~63, raw≈3 -> ~78, raw≈4 -> ~86

# Signal "families" — convergence ACROSS families (filing + surprise + flow) is where edge lives,
# not two of the same kind (two Reddit posts). The brain rewards cross-family agreement.
FAMILY = {
    "edgar_8k": "filing", "edgar_form4": "filing", "edgar_13d": "filing", "newswire": "filing",
    "finnhub_earnings": "surprise",
    "alpaca_news": "news", "finnhub": "news", "google_news": "news", "hackernews": "news",
    "fda": "regulatory", "gov_contract": "regulatory", "index_inclusion": "regulatory",
    "polymarket": "crowd",
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

    def _score(self, sources_strength: dict[str, float], prior: float,
               earliness: float, n_families: int) -> float:
        # raw = sum of (weight * strength) across DISTINCT sources (independence rewarded)
        raw = sum(sources_strength.values())
        base = 100 * (1 - math.exp(-raw / RAW_SCALE))
        # convergence: agreement across DIFFERENT signal families is the real edge
        fbonus = float(self.conv.get("family_bonus", 0.18))
        convergence = min(1.5, 1 + fbonus * max(0, n_families - 1))
        return max(0.0, min(100.0, base * prior * earliness * convergence))

    def _size(self, score: float, capital: float) -> float:
        for band in self.conv["size_bands"]:
            if score >= band["min"]:
                return round(capital * band["frac"], 2)
        return 0.0

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

            # only spend a yfinance price call on candidates that could ACTUALLY qualify
            # (>= min sources AND min families) — at 10k tentacles this prevents a Yahoo ban
            families = {FAMILY.get(s, "other") for s in per_source}
            qualifies = (len(per_source) >= self.conv["min_independent_sources"]
                         and len(families) >= self.conv.get("min_families", 1))
            runup = prices.runup_pct_today(tk) if qualifies else None
            earliness = 0.9 if runup is None else (0.5 if runup > self.conv["max_runup_pct"] else 1.0)
            score = self._score(per_source, ccfg["prior"], earliness, len(families))
            cand = Candidate(
                ticker=tk, catalyst=catalyst, confidence=score,
                size_usd=self._size(score, capital), signals=sigs, runup_pct=runup,
                regime_ok=not regime_down,
                stop_pct=ccfg["stop_pct"], target_pct=ccfg["target_pct"], hold_days=ccfg["hold_days"],
                reasons=[
                    f"{len(per_source)} sources across {len(families)} families "
                    f"({', '.join(sorted(families))}): {', '.join(sorted(per_source))}",
                    f"catalyst={catalyst.value} (prior {ccfg['prior']})",
                    f"runup_today={runup if runup is None else round(runup,1)}% -> earliness {earliness}",
                ],
            )
            cand._families = len(families)  # used by the gate
            out.append(cand)
        out.sort(key=lambda c: c.confidence, reverse=True)
        return out

    def passes_gate(self, c: Candidate) -> tuple[bool, str]:
        if c.n_independent < self.conv["min_independent_sources"]:
            return False, f"only {c.n_independent} source(s); need {self.conv['min_independent_sources']}"
        fams = getattr(c, "_families", 1)
        if fams < self.conv.get("min_families", 1):
            return False, f"only {fams} signal family; need {self.conv['min_families']} (combine signal types)"
        if c.confidence < self.conv["min_score_to_trade"]:
            return False, f"score {c.confidence:.0f} < {self.conv['min_score_to_trade']}"
        if not c.regime_ok:
            return False, "market regime is a downtrend (don't buy longs into a falling tape)"
        return True, "passes conviction gate"
