"""The octopus's tentacles. Each Source.fetch() returns a list[RawSignal].

All sources are DEFENSIVE: a network failure on one tentacle never crashes the bot —
it logs, returns [], and the others keep working (graceful degradation).
"""
from __future__ import annotations

from ..models import RawSignal
from .base import Source
from .free import EdgarSource, ApeWisdomSource, HackerNewsSource, RssSource
from .keyed import AlpacaNewsSource, FinnhubSource
from .extra import (PolymarketSource, UsaSpendingSource, ClinicalTrialsSource,
                    Berkshire13FSource, WikiAttentionSource)
from .newswire import MarketNewsSource
from .fleet import MegaFleetSource
from .scrape import OpenInsiderSource, HtmlScraperSource


def build_sources(cfg: dict, weights: dict) -> list[Source]:
    s = cfg.get("sources", {})
    out: list[Source] = []
    ua = cfg.get("_user_agent", "EARSHOT bot")
    if s.get("edgar"):
        out.append(EdgarSource(weights, ua))
    if s.get("apewisdom"):
        out.append(ApeWisdomSource(weights))
    if s.get("hackernews"):
        out.append(HackerNewsSource(weights))
    if s.get("market_news"):
        out.append(MarketNewsSource(weights))   # OUR own newswire — the free Alpaca/Benzinga replacement
    if s.get("polymarket"):
        out.append(PolymarketSource(weights))
    if s.get("usaspending"):
        out.append(UsaSpendingSource(weights))
    if s.get("clinicaltrials"):
        out.append(ClinicalTrialsSource(weights))
    if s.get("berkshire_13f"):
        out.append(Berkshire13FSource(weights, ua))
    if s.get("openinsider"):
        out.append(OpenInsiderSource(weights))          # scraped whale signal (cluster buys)
    if s.get("wiki_attention"):
        out.append(WikiAttentionSource(weights))        # web-visitor proxy (pageview spikes)
    if s.get("scrape_targets"):
        out.append(HtmlScraperSource(weights, s["scrape_targets"]))
    if s.get("megafleet"):
        out.append(MegaFleetSource(                       # up to ~10k+ tiered rotating tentacles
            weights,
            fast_batch=int(s.get("fleet_fast_batch", 25)),
            wide_batch=int(s.get("fleet_wide_batch", 20)),
            universe=s.get("fleet_universe", "sp500"),
            wide_cap=int(s.get("fleet_wide_cap", 20000))))
    for url in (s.get("rss_feeds") or []):
        out.append(RssSource(url, weights))
    # keyed tentacles activate themselves only if their env keys exist
    out.append(AlpacaNewsSource(weights))
    out.append(FinnhubSource(weights))
    return [src for src in out if src.enabled]


__all__ = ["Source", "RawSignal", "build_sources"]
