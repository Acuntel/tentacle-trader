"""Macro / alternative-data layer — the quant-firm move: don't just read stock headlines, read the
whole economy. Pulls FRED (Federal Reserve Economic Data) series for FREE and KEYLESS via the
fredgraph CSV endpoint — rates, inflation, jobs, growth, credit spreads, sector industrial
production, housing, energy. Derives a MACRO REGIME tag (rate direction + risk-on/off + growth)
that (a) tilts conviction and (b) becomes a PATTERN feature, so the bot learns 'this setup in THIS
regime wins'. FRED has ~800k series — this list is the seed; add IDs to scale the data surface.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import time
import requests

UA = "Mozilla/5.0 (EARSHOT macro)"
CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
FRED_KEY = os.getenv("FRED_API_KEY")   # free at fred.stlouisfed.org/docs/api/api_key.html

# Seed set across rates / inflation / jobs / growth / credit / sectors / housing / energy / markets.
FRED_SERIES = [
    "FEDFUNDS", "DFF", "SOFR", "DGS2", "DGS10", "DGS30", "T10Y2Y", "T10Y3M", "MORTGAGE30US",
    "CPIAUCSL", "CPILFESL", "PCEPI", "PPIACO", "T5YIE", "T10YIE",
    "UNRATE", "PAYEMS", "ICSA", "JTSJOL", "CIVPART",
    "GDPC1", "INDPRO", "TCU", "RSAFS", "UMCSENT", "HOUST", "PERMIT", "DGORDER", "BUSINV",
    "M2SL", "WALCL", "BAMLH0A0HYM2", "BAMLC0A0CM", "TOTALSL", "BUSLOANS", "DRCCLACBS",
    "VIXCLS", "SP500", "NASDAQCOM", "DCOILWTICO", "DHHNGSP", "GASREGW", "DTWEXBGS",
    "IPMAN", "IPMINE", "IPUTIL", "IPDMAN", "IPNMAN", "IPCONGD", "IPBUSEQ", "IPDCONGD",
    "MSPUS", "CSUSHPINSA", "HSN1F", "EXHOSLUSM495S", "RRVRUSQ156N",
    "PCE", "PSAVERT", "DSPIC96", "TOTALSA", "RSXFS",
    # --- farmland / agriculture (commodity + food prices feed agri/fertilizer names) ---
    "PFOODINDEXM", "PMAIZMTUSDM", "PWHEAMTUSDM", "PSOYBUSDM", "CUSR0000SAF11",
    "PORANGUSDM", "PBEEFUSDQ", "APU0000708111", "WPU012",
    # --- housing (national activity + prices) ---
    "HOUST1F", "COMPUTSA", "UNDCONTSA", "MSACSR", "RHORUSQ156N",
    "USSTHPI", "ASPUS", "RRVRUSQ156N", "DRSFRMACBS",
    # --- healthcare ---
    "CPIMEDSL", "CUSR0000SAM", "CUSR0000SAM2", "DHLCRC1Q027SBEA", "PCU6216216211",
]

# FRED tag families to auto-discover MANY more series from (housing/health/farmland/etc.)
EXPAND_TAGS = ["housing", "agriculture", "health", "manufacturing", "employment", "income"]


def _discover(cap: int) -> list[str]:
    """Use the FRED tags API to pull hundreds–thousands more series in the domains we care about."""
    if not FRED_KEY or cap <= 0:
        return []
    ids: list[str] = []
    for tag in EXPAND_TAGS:
        try:
            r = requests.get("https://api.stlouisfed.org/fred/tags/series",
                             params={"tag_names": tag, "api_key": FRED_KEY, "file_type": "json",
                                     "order_by": "popularity", "sort_order": "desc",
                                     "limit": min(1000, cap)}, headers={"User-Agent": UA}, timeout=20)
            ids += [s["id"] for s in r.json().get("seriess", [])]
        except Exception:
            pass
        if len(ids) >= cap:
            break
    return ids[:cap]


def _series(sid: str):
    """Return (latest, prior~6obs-back) for a FRED series. Uses the official API when FRED_API_KEY
    is set (reliable); otherwise tries the keyless CSV (often blocked on cloud IPs)."""
    try:
        if FRED_KEY:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": sid, "api_key": FRED_KEY, "file_type": "json",
                                     "sort_order": "desc", "limit": 7},
                             headers={"User-Agent": UA}, timeout=12)
            obs = [float(o["value"]) for o in r.json().get("observations", []) if o["value"] not in (".", "")]
            return (obs[0], obs[-1]) if obs else (None, None)   # desc: [0]=latest, [-1]=~6 back
        r = requests.get(CSV + sid, headers={"User-Agent": UA}, timeout=12)
        vals = []
        for ln in r.text.splitlines()[1:]:
            parts = ln.split(",")
            if len(parts) >= 2 and parts[1] not in (".", "", "NA"):
                try:
                    vals.append(float(parts[1]))
                except ValueError:
                    pass
        if not vals:
            return None, None
        return vals[-1], (vals[-7] if len(vals) >= 7 else vals[0])
    except Exception:
        return None, None


class MacroMonitor:
    """Self-throttling (refreshes ~daily). Holds a compact regime tag + the raw latest values.
    With a FRED key it auto-discovers thousands more series (housing/health/farmland/...)."""

    def __init__(self, expand_cap: int = 0):
        self.series = list(dict.fromkeys(FRED_SERIES))
        self.expand_cap = expand_cap
        self.context = ""
        self.values: dict = {}
        self._last = 0.0
        self._expanded = False

    @property
    def series_count(self) -> int:
        return len(self.series)

    def update(self):
        if time.time() - self._last < 86400 and self.context:
            return                                            # macro moves slowly; refresh daily
        self._last = time.time()
        if self.expand_cap and not self._expanded and FRED_KEY:
            self.series = list(dict.fromkeys(self.series + _discover(self.expand_cap)))
            self._expanded = True
        pulled = {}
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            for sid, res in zip(self.series, ex.map(_series, self.series)):
                if res[0] is not None:
                    pulled[sid] = res
        self.values = pulled
        self.context = self._regime(pulled)
        print(f"  [macro] {len(pulled)}/{self.series_count} FRED series -> regime: {self.context or 'unknown'}")

    @staticmethod
    def _regime(v: dict) -> str:
        tags = []
        ff = v.get("FEDFUNDS") or v.get("DFF")
        if ff:
            tags.append("rates_down" if ff[0] < ff[1] - 0.02 else
                        "rates_up" if ff[0] > ff[1] + 0.02 else "rates_flat")
        vix = v.get("VIXCLS")
        curve = v.get("T10Y2Y")
        risk_off = (vix and vix[0] > 22) or (curve and curve[0] < 0)
        tags.append("risk_off" if risk_off else "risk_on")
        ip = v.get("INDPRO")
        if ip:
            tags.append("growth_up" if ip[0] >= ip[1] else "growth_down")
        return "|".join(tags)
