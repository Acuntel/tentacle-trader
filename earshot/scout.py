"""The Source Scout — the octopus grows new tentacles. It scores how good each existing
source has been and, when one decays or coverage is thin, proposes new FREE sources to add
(at probationary low weight, shadow-only). Live web-discovery is a hook (wired to WebSearch
in the agent runtime); the curated fallbacks below are real, free endpoints."""
from __future__ import annotations

# Free sources the Scout can propose, keyed by the gap they fill.
CANDIDATE_SOURCES = {
    "smart_money": [
        ("sec_13f_berkshire", "EDGAR 13F — track Berkshire/Buffett CIK directly"),
        ("capitol_trades", "Congress trades (free)"),
        ("hedgefollow", "Free guru/whale portfolio tracker"),
    ],
    "regulatory": [
        ("usaspending", "USASpending.gov federal contract awards API"),
        ("clinicaltrials", "ClinicalTrials.gov status changes (biotech)"),
        ("openfda", "openFDA drug approvals"),
    ],
    "crowd_forecast": [
        ("polymarket", "Polymarket odds — often lead the news"),
        ("kalshi", "Kalshi event markets"),
        ("wikipedia_pageviews", "Attention spikes via Wikipedia pageview API"),
        ("google_trends", "Search-interest spikes (pytrends)"),
    ],
    "ai_quantum": [
        ("github_trending", "GitHub Trending — viral repos move related names"),
        ("huggingface_trending", "Hugging Face Trending models"),
        ("arxiv", "arXiv new AI/quantum papers"),
    ],
    "x_around": [
        ("rsshub_x", "Self-hosted RSSHub route for an X account (the X-around)"),
        ("bluesky", "Bluesky firehose (free, open API)"),
        ("mastodon", "Mastodon finance servers (free API)"),
    ],
}


class SourceScout:
    def __init__(self, know: dict, weights: dict):
        self.know = know
        self.weights = weights

    def report(self) -> dict:
        """Per-source precision + lead quality, and which sources are decaying."""
        rows = []
        for src, st in self.know.get("sources", {}).items():
            t = st["trades"]
            if t == 0:
                continue
            rows.append({
                "source": src, "trades": t, "precision": round(st["wins"] / t, 2),
                "avg_ret": round(st["sum_ret"] / t, 2), "weight": self.weights.get(src, 0.5),
            })
        rows.sort(key=lambda r: r["precision"])
        weak = [r["source"] for r in rows if r["trades"] >= 8 and r["precision"] < 0.4]
        return {"sources": rows, "decaying": weak}

    def propose(self, gap: str | None = None) -> list[tuple[str, str]]:
        """Suggest new free tentacles. In the agent runtime this is backed by a live web search;
        here it returns curated, real free endpoints to wire next."""
        if gap and gap in CANDIDATE_SOURCES:
            return CANDIDATE_SOURCES[gap]
        return [c for v in CANDIDATE_SOURCES.values() for c in v]
