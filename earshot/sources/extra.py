"""§18 heavy-hitter free tentacles: crowd-forecast (Polymarket), government contracts
(USASpending), biotech (ClinicalTrials), and whale 13F holdings (Berkshire/Buffett)."""
from __future__ import annotations

import json
import re
import requests

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers, NAME_TICKER

TIMEOUT = 15

# Public companies that win big federal contracts / make drugs — name -> ticker.
CONTRACTOR_TICKER = {
    "lockheed": "LMT", "raytheon": "RTX", "rtx": "RTX", "boeing": "BA",
    "northrop": "NOC", "general dynamics": "GD", "l3harris": "LHX", "palantir": "PLTR",
    "leidos": "LDOS", "booz allen": "BAH", "saic": "SAIC", "kbr": "KBR",
    "huntington ingalls": "HII", "axon": "AXON", "honeywell": "HON",
}
PHARMA_TICKER = {
    "pfizer": "PFE", "moderna": "MRNA", "merck": "MRK", "eli lilly": "LLY", "lilly": "LLY",
    "amgen": "AMGN", "gilead": "GILD", "biogen": "BIIB", "vertex": "VRTX", "regeneron": "REGN",
    "bristol myers": "BMY", "johnson & johnson": "JNJ", "abbvie": "ABBV", "novavax": "NVAX",
    "sarepta": "SRPT", "bluebird": "BLUE", "intellia": "NTLA", "crispr": "CRSP",
}
BERKSHIRE_CIK = "0001067983"


def _map(name: str, table: dict) -> str | None:
    low = (name or "").lower()
    for k, tk in table.items():
        if k in low:
            return tk
    return None


class PolymarketSource(Source):
    """Crowd-forecast — prediction-market odds often shift before the news. Confirmation, low weight."""
    key = "polymarket"

    def fetch(self) -> list[RawSignal]:
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&order=volume"
                         "&ascending=false&limit=80", timeout=TIMEOUT)
        out: list[RawSignal] = []
        for m in r.json():
            q = m.get("question", "")
            tickers = extract_tickers(q) or ([_map(q, NAME_TICKER)] if _map(q, NAME_TICKER) else [])
            tickers = [t for t in tickers if t]
            if not tickers:
                continue
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                edge = abs(float(prices[0]) - 0.5) * 2 if prices else 0  # 0 (toss-up) .. 1 (near-certain)
            except Exception:
                edge = 0
            if edge < 0.4:
                continue
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(), tickers=tickers,
                headline=f"Polymarket: {q} (conviction {edge:.0%})", url="https://polymarket.com",
                strength=min(2.0, 0.5 + edge),
            ))
        return out


class UsaSpendingSource(Source):
    """Federal contract awards — a public company landing a big award is a real catalyst."""
    key = "gov_contract"

    def fetch(self) -> list[RawSignal]:
        body = {
            "filters": {"award_type_codes": ["A", "B", "C", "D"],
                        "time_period": [{"start_date": "2020-01-01", "end_date": "2030-01-01"}]},
            "fields": ["Award Amount", "Recipient Name", "Award ID"],
            "sort": "Award Amount", "order": "desc", "limit": 40,
        }
        r = requests.post("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                          json=body, timeout=TIMEOUT)
        out: list[RawSignal] = []
        for a in r.json().get("results", []):
            name = a.get("Recipient Name", "")
            tk = _map(name, CONTRACTOR_TICKER)
            amt = a.get("Award Amount", 0) or 0
            if tk and amt >= 50_000_000:
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{name} federal award ${amt/1e6:.0f}M", url="https://usaspending.gov",
                    catalyst_hint="gov_contract", strength=min(2.0, 0.6 + amt / 1e9),
                ))
        return out


class ClinicalTrialsSource(Source):
    """Biotech trial status changes precede drug news. Public sponsors only."""
    key = "fda"

    def fetch(self) -> list[RawSignal]:
        r = requests.get("https://clinicaltrials.gov/api/v2/studies?sort=LastUpdatePostDate:desc"
                         "&pageSize=60&fields=protocolSection.identificationModule,"
                         "protocolSection.statusModule,protocolSection.sponsorCollaboratorsModule",
                         timeout=TIMEOUT)
        out: list[RawSignal] = []
        for s in r.json().get("studies", []):
            ps = s.get("protocolSection", {})
            sponsor = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")
            status = ps.get("statusModule", {}).get("overallStatus", "")
            tk = _map(sponsor, PHARMA_TICKER)
            if tk and status in ("COMPLETED", "ACTIVE_NOT_RECRUITING"):
                title = ps.get("identificationModule", {}).get("briefTitle", "")
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{sponsor} trial {status}: {title[:80]}",
                    url="https://clinicaltrials.gov", catalyst_hint="fda_approval", strength=0.8,
                ))
        return out


class Berkshire13FSource(Source):
    """Track Buffett: emit Berkshire's reported holdings on a NEW 13F (position-following, low weight,
    45-day lag — never a latency play). Persists last-seen accession to fire only on changes."""
    key = "edgar_13d"  # reuse a whale weight bucket
    STATE = "state/berkshire13f.json"

    def __init__(self, weights: dict, user_agent: str):
        super().__init__(weights)
        self.ua = user_agent

    def fetch(self) -> list[RawSignal]:
        h = {"User-Agent": self.ua}
        sub = requests.get(f"https://data.sec.gov/submissions/CIK{BERKSHIRE_CIK}.json",
                           headers=h, timeout=TIMEOUT).json()
        recent = sub.get("filings", {}).get("recent", {})
        forms, accns = recent.get("form", []), recent.get("accessionNumber", [])
        acc = next((accns[i] for i, f in enumerate(forms) if f.startswith("13F")), None)
        if not acc:
            return []
        try:
            last = json.load(open(self.STATE)).get("acc")
        except Exception:
            last = None
        if acc == last:
            return []  # already processed this quarter's filing
        adash = acc.replace("-", "")
        idx = requests.get(f"https://www.sec.gov/Archives/edgar/data/{int(BERKSHIRE_CIK)}/{adash}/index.json",
                           headers=h, timeout=TIMEOUT).json()
        info = next((it["name"] for it in idx.get("directory", {}).get("item", [])
                     if it["name"].endswith(".xml") and "info" in it["name"].lower()), None)
        out: list[RawSignal] = []
        if info:
            xml = requests.get(f"https://www.sec.gov/Archives/edgar/data/{int(BERKSHIRE_CIK)}/{adash}/{info}",
                               headers=h, timeout=TIMEOUT).text
            names = re.findall(r"<nameOfIssuer>(.*?)</nameOfIssuer>", xml, re.I)
            seen = set()
            for nm in names:
                tk = _map(nm, NAME_TICKER) or _map(nm, PHARMA_TICKER) or _map(nm, CONTRACTOR_TICKER)
                if tk and tk not in seen:
                    seen.add(tk)
                    out.append(RawSignal(
                        source=self.key, weight=self.weight() * 0.6, ts=now_ts(), tickers=[tk],
                        headline=f"Berkshire 13F holds {nm} ({tk})", url="https://sec.gov",
                        catalyst_hint="activist_stake", strength=0.6,
                    ))
        try:
            import os
            os.makedirs("state", exist_ok=True)
            json.dump({"acc": acc}, open(self.STATE, "w"))
        except Exception:
            pass
        return out
