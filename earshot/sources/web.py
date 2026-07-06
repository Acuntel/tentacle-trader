"""Web-demand tentacles — App Store review velocity/sentiment (free) + Cloudflare Radar traffic (keyed).

Review COUNT velocity = engagement growth; average rating = product sentiment. The iTunes Search API
resolves a ticker's app and returns userRatingCount + averageUserRating for free (no hardcoded app ids).
Cloudflare Radar gives real domain-traffic rank trend — the free Similarweb proxy — but needs a free
CLOUDFLARE_API_TOKEN; it stays dark until that's set (like FRED/Finnhub). Both baseline-delta. Family
'demand'.
"""
from __future__ import annotations

import os
import requests

from ..models import RawSignal, now_ts
from .base import Source
from .altdata import _load, _save
from .demand import APP_NAME_TICKER

TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (TENTACLE web research)"}


class AppStoreReviewSource(Source):
    """iTunes Search API -> review-count velocity + average-rating sentiment per consumer-app ticker."""
    key = "appreviews"
    min_interval = 7200.0           # 2h; ratings move slowly
    PATH = "state/web_appreviews.json"

    def __init__(self, weights, batch=8):
        super().__init__(weights)
        self.names = list(dict.fromkeys(APP_NAME_TICKER.keys()))
        self.i = 0
        self.batch = batch

    def fetch(self) -> list[RawSignal]:
        prev = _load(self.PATH, {})
        out = []
        picks = [self.names[(self.i + k) % len(self.names)] for k in range(self.batch)]
        self.i = (self.i + self.batch) % len(self.names)
        for name in picks:
            tk = APP_NAME_TICKER.get(name)
            if not tk:
                continue
            try:
                # AUDIT FIX: results[0] blindly trusted iTunes search relevance — the term "block"
                # returned "Block Blast!" (an unrelated game with 2.57M ratings) attributed to the
                # ticker. Take the first result whose trackName ANCHORS to the queried flagship name.
                r = requests.get("https://itunes.apple.com/search",
                                 params={"term": name, "entity": "software", "country": "us", "limit": 5},
                                 headers=UA, timeout=TIMEOUT)
                res = None
                for cand in (r.json().get("results") or []):
                    tn = (cand.get("trackName") or "").lower().strip()
                    if (tn == name or tn.startswith(name + " - ") or tn.startswith(name + ": ")
                            or tn.startswith(name + " – ")):
                        res = cand
                        break
                if not res:
                    continue
                count = int(res.get("userRatingCount") or 0)
                rating = float(res.get("averageUserRating") or 0)
            except Exception:
                continue
            if not count:
                continue
            old = prev.get(name)
            if old and count > old * 1.05 and count - old >= 2000 and rating >= 4.0:
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{tk} app engagement up — {name} reviews {old:,}→{count:,} "
                             f"(rating {rating:.1f}★)",
                    url="https://apps.apple.com", strength=min(1.8, 0.5 + (count / old - 1) * 5)))
            prev[name] = count
        _save(self.PATH, prev)
        return out


# company domain -> ticker (Cloudflare Radar rank trend = web-traffic demand)
DOMAINS = {"netflix.com": "NFLX", "shopify.com": "SHOP", "airbnb.com": "ABNB", "uber.com": "UBER",
           "doordash.com": "DASH", "coinbase.com": "COIN", "robinhood.com": "HOOD", "etsy.com": "ETSY",
           "chewy.com": "CHWY", "carvana.com": "CVNA", "pinterest.com": "PINS", "reddit.com": "RDDT",
           "roblox.com": "RBLX", "spotify.com": "SPOT", "duolingo.com": "DUOL", "draftkings.com": "DKNG"}


class CloudflareRadarSource(Source):
    """Domain-traffic rank trend from Cloudflare Radar (free token). Dark until CLOUDFLARE_API_TOKEN set."""
    key = "web_traffic"
    min_interval = 21600.0
    PATH = "state/web_cfradar.json"

    def __init__(self, weights, batch=6):
        super().__init__(weights)
        self.token = os.getenv("CLOUDFLARE_API_TOKEN")
        self.enabled = bool(self.token)
        if not self.enabled:
            print("  [tentacle:web_traffic] inactive (no CLOUDFLARE_API_TOKEN)")
        self.domains = list(DOMAINS.keys())
        self.i = 0
        self.batch = batch

    def fetch(self) -> list[RawSignal]:
        if not self.token:
            return []
        prev = _load(self.PATH, {})
        out = []
        hdr = {**UA, "Authorization": f"Bearer {self.token}"}
        picks = [self.domains[(self.i + k) % len(self.domains)] for k in range(self.batch)]
        self.i = (self.i + self.batch) % len(self.domains)
        for dom in picks:
            try:
                r = requests.get("https://api.cloudflare.com/client/v4/radar/ranking/domain/" + dom,
                                 params={"limit": 1}, headers=hdr, timeout=TIMEOUT)
                series = (((r.json() or {}).get("result") or {}).get("details_0") or {})
                rank = int(series.get("rank") or 0)
            except Exception:
                continue
            if not rank:
                continue
            old = prev.get(dom)
            if old and old - rank >= max(50, int(old * 0.1)):   # a real rank climb (lower = more traffic)
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[DOMAINS[dom]],
                    headline=f"{DOMAINS[dom]} web traffic rising — {dom} Cloudflare rank {old:,}→{rank:,}",
                    url="https://radar.cloudflare.com", strength=min(1.8, 0.6 + (old - rank) / old)))
            prev[dom] = rank
        _save(self.PATH, prev)
        return out
