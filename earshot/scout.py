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

# Verified-dead or already-done proposals — NEVER re-propose these. Each entry was checked by
# hand; leaving them in the queue burned the coach LLM's attention on the same corpses every pass.
DEAD_SOURCES = {
    "sec_13f_berkshire": "already implemented — sources/extra.py Berkshire13FSource (config berkshire_13f: true)",
    "capitol_trades": "Cloudflare 429, not freely scrapeable, verified 2026-07-13",
    "hedgefollow": "fund pages 404 + 13F data is 45-day lagged anyway, verified 2026-07-13",
    "house_stock_watcher": "S3 bucket 403 since 2026",
}
DEAD_STRIKES = 3    # probe failures on this many SEPARATE Scout passes -> auto-dead


class SourceScout:
    def __init__(self, know: dict, weights: dict):
        self.know = know
        self.weights = weights

    def _dead(self) -> dict:
        """Hand-verified blocklist + the self-extending know['scout_dead'] ledger, name -> reason."""
        dead = dict(DEAD_SOURCES)
        dead.update(self.know.get("scout_dead") or {})
        return dead

    def _prune_proposed(self):
        """Drop dead names from know['proposed_sources'] so the coach stops re-reading them forever."""
        dead = self._dead()
        queued = self.know.get("proposed_sources")
        if queued:
            self.know["proposed_sources"] = [s for s in queued if s not in dead]

    def note_probe_failure(self, name: str, reason: str):
        """A proposed source's probe endpoint failed this pass. DEAD_STRIKES failures on separate
        Scout passes auto-move it to the know['scout_dead'] ledger (with the reason) — a dead idea
        stops being re-proposed instead of clogging the queue forever."""
        if name in self._dead():                           # already buried — don't restart the count
            return
        cur = int(self.know.get("scout_pass", 0))
        fails = self.know.setdefault("scout_probe_fails", {})
        rec = fails.setdefault(name, {"strikes": 0, "reason": "", "last_pass": None})
        if rec.get("last_pass") == cur:                    # one strike per Scout pass, not per retry
            return
        rec.update(strikes=rec["strikes"] + 1, reason=reason, last_pass=cur)
        if rec["strikes"] >= DEAD_STRIKES:
            self.know.setdefault("scout_dead", {})[name] = f"probe failed {rec['strikes']}x: {reason}"
            fails.pop(name, None)
            self._prune_proposed()

    def report(self) -> dict:
        """Per-source precision + lead quality, and which sources are decaying."""
        self.know["scout_pass"] = int(self.know.get("scout_pass", 0)) + 1   # a new Scout pass begins
        self._prune_proposed()
        rows = []
        for src, st in self.know.get("sources", {}).items():
            t = st["trades"]
            if t == 0:
                continue
            avg = st["sum_ret"] / t
            if avg != avg:                     # AUTOPSY FIX 2026-09-01: NaN sum_ret (google_news/form4)
                avg = 0.0
                st["sum_ret"] = 0.0
            rows.append({
                "source": src, "trades": t, "precision": round(st["wins"] / t, 2),
                "avg_ret": round(avg, 2), "weight": self.weights.get(src, 0.5),
            })
        rows.sort(key=lambda r: r["precision"], reverse=True)
        weak = [r["source"] for r in rows if r["trades"] >= 8 and r["precision"] < 0.4]
        return {"sources": rows, "decaying": weak}

    def propose(self, gap: str | None = None) -> list[tuple[str, str]]:
        """Suggest new free tentacles. In the agent runtime this is backed by a live web search;
        here it returns curated, real free endpoints to wire next. Verified-dead / already-done
        names (DEAD_SOURCES + know['scout_dead']) are filtered out and pruned from the queue."""
        dead = self._dead()
        self._prune_proposed()
        if gap and gap in CANDIDATE_SOURCES:
            cands = CANDIDATE_SOURCES[gap]
        else:
            cands = [c for v in CANDIDATE_SOURCES.values() for c in v]
        return [(name, desc) for name, desc in cands if name not in dead]
