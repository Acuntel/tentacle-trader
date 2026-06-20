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
                    Berkshire13FSource, WikiAttentionSource, NasdaqHaltsSource)
from .newswire import MarketNewsSource
from .fleet import MegaFleetSource
from .scrape import OpenInsiderSource, HtmlScraperSource
from .altdata import SecFullTextSource, GreenhouseHiringSource
from .demand import AppStoreDemandSource, SteamDemandSource, GithubActivitySource
from .adoption import PyPiDownloadsSource, NpmDownloadsSource, DockerPullsSource
from .onchain import OnChainCryptoSource
from .hiring import MultiAtsHiringSource
from .web import AppStoreReviewSource, CloudflareRadarSource
from .jets import JetSpottingSource
from .congress import CongressTradesSource


def build_sources(cfg: dict, weights: dict) -> list[Source]:
    s = cfg.get("sources", {})
    out: list[Source] = []
    ua = cfg.get("_user_agent", "TENTACLE bot")
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
    if s.get("nasdaq_halts"):
        out.append(NasdaqHaltsSource(weights))          # live trading halts / resumptions (volatility catalyst)
    if s.get("sec_fulltext"):
        out.append(SecFullTextSource(weights, ua))      # EDGAR full-text phrase search (scrape pack)
    if s.get("hiring"):
        out.append(GreenhouseHiringSource(weights))     # hiring-velocity alt-data (scrape pack)
    if s.get("appstore"):
        out.append(AppStoreDemandSource(weights))       # App Store rank demand (free primary)
    if s.get("steam"):
        out.append(SteamDemandSource(weights))          # Steam live-player usage surge
    if s.get("github"):
        out.append(GithubActivitySource(weights))       # dev-tool adoption (star velocity)
    if s.get("pypi"):
        out.append(PyPiDownloadsSource(weights))        # package adoption (PyPI download velocity)
    if s.get("npm"):
        out.append(NpmDownloadsSource(weights))         # package adoption (npm)
    if s.get("docker"):
        out.append(DockerPullsSource(weights))          # package adoption (Docker pulls)
    if s.get("onchain"):
        out.append(OnChainCryptoSource(weights))        # crypto read-through to COIN/MSTR/miners
    if s.get("hiring_ats"):
        out.append(MultiAtsHiringSource(weights))       # Lever/Ashby/SmartRecruiters hiring velocity
    if s.get("appreviews"):
        out.append(AppStoreReviewSource(weights))       # App Store review velocity + sentiment
    if s.get("web_traffic"):
        out.append(CloudflareRadarSource(weights))      # web-traffic rank (keyed; dark w/o CLOUDFLARE_API_TOKEN)
    if s.get("jets"):
        out.append(JetSpottingSource(weights))          # ADS-B corporate-jet movement (needs verified registry)
    if s.get("congress"):
        out.append(CongressTradesSource(weights))       # politician trades (keyed; dark w/o FMP_API_KEY)
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
