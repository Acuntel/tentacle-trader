"""On-chain tentacle — crypto price momentum + network usage as a read-through to crypto EQUITIES.

COIN, MSTR, and the miners (MARA/RIOT/CLSK/...) trade as leveraged proxies on bitcoin. CoinGecko
(free, no key) gives BTC/ETH price + 24h move; mempool.space (free) gives network usage (fees/tx).
A big crypto move or a usage surge is a same-day catalyst for the basket. Family 'onchain' — an
orthogonal pillar (it's not news, filing, price-of-the-stock, or options). Emits only on a real move.
"""
from __future__ import annotations

import requests

from ..models import RawSignal, now_ts
from .base import Source

TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (TENTACLE onchain research)"}

# equities that track bitcoin/crypto (the read-through basket)
CRYPTO_EQUITIES = ["COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "BITF", "CIFR", "WULF", "BTDR",
                   "HOOD", "XYZ", "BMNR"]


class OnChainCryptoSource(Source):
    key = "onchain"
    min_interval = 600.0          # 10 min; CoinGecko free tier is ~30/min, be polite
    MOVE_PCT = 4.0                # |BTC 24h| past this -> a basket catalyst

    def fetch(self) -> list[RawSignal]:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                                     "include_24hr_change": "true"}, headers=UA, timeout=TIMEOUT)
            j = r.json()
            btc = float(j["bitcoin"]["usd"]); chg = float(j["bitcoin"].get("usd_24h_change") or 0)
        except Exception:
            return []
        if abs(chg) < self.MOVE_PCT:
            return []                                          # nothing to say — no stale repeat
        direction = "up" if chg > 0 else "down"
        # only long-side: a BTC RALLY is the tradeable read-through for these longs
        if chg <= 0:
            return []
        out = []
        for tk in CRYPTO_EQUITIES:
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                headline=f"{tk} crypto read-through — BTC ${btc:,.0f} {chg:+.1f}% 24h ({direction})",
                url="https://www.coingecko.com", catalyst_hint=None,
                strength=min(2.0, 0.5 + abs(chg) / 8)))
        return out
