"""ADS-B jet-spotting — corporate-jet movement as an M&A / site-visit tell (OpenSky, free).

⚠️ HONEST: the DATA PIPE works (OpenSky returns live aircraft for free), but the value depends entirely
on a VERIFIED icao-hex -> company -> ticker registry, and that map is the hard, unreliable part. We do
NOT ship fabricated tail numbers (the cardinal sin: a wrong data point that looks authoritative). So the
registry ships EMPTY and the tentacle self-disables until it's populated — build the map from the FREE
FAA Releasable Aircraft Database (registry.faa.gov, N-number -> registered owner) or ADSB Exchange owner
tags, then drop {icao_hex: ticker} entries below. Baseline-delta: emit when a tracked jet is newly
airborne. Family 'flow' (speculative, low weight).
"""
from __future__ import annotations

import requests

from ..models import RawSignal, now_ts
from .base import Source
from .altdata import _load, _save

TIMEOUT = 15
UA = {"User-Agent": "Mozilla/5.0 (TENTACLE adsb research)"}

# icao24 (lowercase hex) -> ticker. EMPTY until populated from a VERIFIED source (FAA registry).
# Example shape (do not trust these without verifying against the FAA N-number lookup):
#   "a1b2c3": "TICKER",
JET_REGISTRY: dict[str, str] = {}


class JetSpottingSource(Source):
    """Watches a verified set of corporate-jet ICAO hexes for new airborne activity. No-op if unmapped."""
    key = "jets"
    min_interval = 1800.0
    PATH = "state/jets_seen.json"
    API = "https://opensky-network.org/api/states/all"

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.enabled = bool(JET_REGISTRY)
        if not self.enabled:
            print("  [tentacle:jets] inactive (JET_REGISTRY empty — needs a verified FAA-derived map)")

    def fetch(self) -> list[RawSignal]:
        if not JET_REGISTRY:
            return []
        try:
            r = requests.get(self.API, params={"icao24": list(JET_REGISTRY.keys())},
                             headers=UA, timeout=TIMEOUT)
            states = (r.json() or {}).get("states") or []
        except Exception:
            return []
        prev = _load(self.PATH, {})
        out, now = [], now_ts()
        for st in states:
            icao = (st[0] or "").lower()
            on_ground = bool(st[8])
            tk = JET_REGISTRY.get(icao)
            if not tk or on_ground:
                continue
            if now - float(prev.get(icao, 0)) < 6 * 3600:      # one ping per jet per 6h (de-noise)
                continue
            prev[icao] = now
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now, tickers=[tk],
                headline=f"{tk} corporate jet airborne (ICAO {icao}) — possible M&A/site-visit",
                url="https://opensky-network.org", strength=0.7))
        _save(self.PATH, prev)
        return out
