"""Evidence dossier + TRUE independence — the substrate the analyst reasons over.

"2 sources agree" is shallow when both echo the same newswire. This assembles everything we know
about a candidate — semantically-deduped news EVENTS (not headline copies), the filing, price action,
the options tape, fundamentals, macro — and scores independence by ORTHOGONAL evidence TYPE (filing +
price + options + fundamentals), not by raw source count. That score is what real confirmation means.
"""
from __future__ import annotations

import re

from . import technicals, options, fundamentals

_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "s", "inc", "corp", "co",
         "ltd", "is", "at", "as", "by", "with", "its", "new", "says", "after", "amid", "will"}


def _norm(t: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", (t or "").lower()).split()
            if w not in _STOP and len(w) > 2]


def _jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def _distinct_events(headlines: list[str]) -> int:
    """Collapse echoed copies of the SAME story (google_news + yahoo + wire) into one event."""
    events: list[list[str]] = []
    for h in headlines:
        toks = _norm(h)
        if not toks or any(_jaccard(toks, e) > 0.55 for e in events):
            continue
        events.append(toks)
    return len(events)


def build(candidate, macro: str = "") -> dict:
    """Assemble the dossier + independence score for one already-qualifying candidate."""
    from .brain import FAMILY                              # lazy import to avoid a cycle
    fams: dict[str, list[str]] = {}
    for s in candidate.signals:
        fams.setdefault(FAMILY.get(s.source, "other"), []).append(s.headline)

    news_events = _distinct_events(tuple(fams.get("news", [])))
    # Filing headlines are distinct semantic events even if Jaccard overlaps with news.
    # AUDIT FIX: this was computed and thrown away (write-only) — now returned + featurized.
    filing_events = len(fams.get("filing", []))
    tech = technicals.read(candidate.ticker)
    opt = options.read(candidate.ticker)
    fund = fundamentals.read(candidate.ticker)

    # PILLARS = orthogonal evidence TYPES that agree (this is the real confirmation count)
    pillars: set[str] = set()
    if fams.get("filing"):
        pillars.add("filing")
    if fams.get("news") and news_events >= 1:
        pillars.add("news")
    if fams.get("regulatory"):
        pillars.add("regulatory")
    if fams.get("crowd") or fams.get("social") or fams.get("attention"):
        pillars.add("crowd")
    if fams.get("alt"):
        pillars.add("altdata")
    if fams.get("demand"):
        pillars.add("demand")                            # consumer usage/adoption — an orthogonal pillar
    if fams.get("onchain"):
        pillars.add("onchain")                           # crypto network read-through
    if fams.get("flow"):
        pillars.add("flow")                              # jet/flow positioning
    if tech and tech.get("bias", 0) > 0.15:
        pillars.add("price")
    if opt and opt.get("bias", 0) >= 0.25:
        pillars.add("options")
    if fund and fund.get("squeeze", 0) >= 0.5:
        pillars.add("squeeze")
    if fund and fund.get("quality", 0) >= 0.6:
        pillars.add("quality")

    return {"ticker": candidate.ticker, "catalyst": candidate.catalyst.value, "macro": macro,
            "pillars": sorted(pillars), "independence": len(pillars), "news_events": news_events,
            "filing_events": filing_events,
            "n_sources": candidate.n_independent, "tech": tech, "options": opt, "fundamentals": fund,
            "headlines": [s.headline for s in candidate.signals][:6]}
