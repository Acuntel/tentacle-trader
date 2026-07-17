"""§18 heavy-hitter free tentacles: crowd-forecast (Polymarket), government contracts
(USASpending), biotech (ClinicalTrials), and whale 13F holdings (Berkshire/Buffett)."""
from __future__ import annotations

import datetime
import json
import re
import requests

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers, NAME_TICKER, SeenSet
from .resolver import resolve_name

TIMEOUT = 15

# Public companies that win big federal contracts / make drugs — name -> ticker.
CONTRACTOR_TICKER = {
    "lockheed": "LMT", "raytheon": "RTX", "rtx": "RTX", "boeing": "BA",
    "northrop": "NOC", "general dynamics": "GD", "l3harris": "LHX", "palantir": "PLTR",
    "leidos": "LDOS", "booz allen": "BAH", "saic": "SAIC", "kbr": "KBR",
    "huntington ingalls": "HII", "axon": "AXON", "honeywell": "HON",
    # federal-only subsidiaries whose legal names the SEC resolver can't see (parent is public):
    "accenture": "ACN", "caci": "CACI", "hii mission": "HII", "maximus": "MMS",
}
PHARMA_TICKER = {
    "pfizer": "PFE", "moderna": "MRNA", "merck": "MRK", "eli lilly": "LLY", "lilly": "LLY",
    "amgen": "AMGN", "gilead": "GILD", "biogen": "BIIB", "vertex": "VRTX", "regeneron": "REGN",
    "bristol myers": "BMY", "johnson & johnson": "JNJ", "abbvie": "ABBV", "novavax": "NVAX",
    "sarepta": "SRPT", "bluebird": "BLUE", "intellia": "NTLA", "crispr": "CRSP",
}
BERKSHIRE_CIK = "0001067983"


def _map(name: str, table: dict) -> str | None:
    """Hand-table lookup (aliases/substrings). Callers fall back to resolver.resolve_name —
    the SEC 12k-issuer map — AFTER every hand table misses, so the curated aliases stay first."""
    low = (name or "").lower()
    for k, tk in table.items():
        if k in low:
            return tk
    return None


class PolymarketSource(Source):
    """Crowd-forecast — prediction-market odds often shift before the news. Confirmation, low weight."""
    key = "polymarket"
    min_interval = 120.0

    def fetch(self) -> list[RawSignal]:
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&order=volume"
                         "&ascending=false&limit=80", timeout=TIMEOUT)
        out: list[RawSignal] = []
        for m in r.json():
            q = m.get("question", "")
            tk = _map(q, NAME_TICKER) or resolve_name(q)
            tickers = extract_tickers(q) or ([tk] if tk else [])
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
    """Federal contract awards — a public company landing a big award is a real catalyst.
    AUDIT FIX: the query used to ask for the ALL-TIME (2020-2030) top-40 by amount — the same
    mega-awards from years past, re-emitted as 'news' every hour forever. Now: awards ACTED ON in
    the last 7 days (a real catalyst window), each emitted once via a persisted Award-ID seen-set."""
    key = "gov_contract"
    min_interval = 3600.0        # contract data updates ~daily

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.seen = SeenSet("usaspending")

    def fetch(self) -> list[RawSignal]:
        today = datetime.date.today()
        body = {
            "filters": {"award_type_codes": ["A", "B", "C", "D"],
                        "time_period": [{"start_date": (today - datetime.timedelta(days=7)).isoformat(),
                                         "end_date": today.isoformat(),
                                         "date_type": "action_date"}]},
            "fields": ["Award Amount", "Recipient Name", "Award ID"],
            "sort": "Award Amount", "order": "desc", "limit": 40,
        }
        r = requests.post("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                          json=body, timeout=TIMEOUT)
        out: list[RawSignal] = []
        for a in r.json().get("results", []):
            name = a.get("Recipient Name", "")
            tk = _map(name, CONTRACTOR_TICKER) or resolve_name(name)
            amt = a.get("Award Amount", 0) or 0
            aid = a.get("Award ID") or f"{name}|{amt}"
            if tk and amt >= 50_000_000 and not self.seen.seen(aid):
                self.seen.add(aid)
                # RELATIVE surprise (EDGE doctrine): a $50M award to a $100M-cap company is a
                # thesis; $2B to Lockheed is a rounding error. Absolute dollars only as fallback
                # when fundamentals aren't cached. fundamentals.read is 6h-cached (new awards
                # are seen-gated, so this stays cheap).
                from .. import fundamentals
                mc = ((fundamentals.read(tk) or {}).get("market_cap") or 0)
                if mc > 0:
                    strength = min(2.0, 0.5 + 8.0 * (amt / mc))
                    pct = f" ({amt / mc:.0%} of mktcap)"
                else:
                    strength = min(2.0, 0.6 + amt / 1e9)
                    pct = ""
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{name} federal award ${amt/1e6:.0f}M{pct}",
                    url="https://usaspending.gov",
                    catalyst_hint="gov_contract", strength=strength,
                ))
        return out


class ClinicalTrialsSource(Source):
    """Biotech trial status changes precede drug news. Public sponsors only."""
    key = "fda"
    min_interval = 3600.0

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
            tk = _map(sponsor, PHARMA_TICKER) or resolve_name(sponsor)
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
    min_interval = 21600.0       # quarterly filing; checking every 6h is plenty
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
                tk = (_map(nm, NAME_TICKER) or _map(nm, PHARMA_TICKER)
                      or _map(nm, CONTRACTOR_TICKER) or resolve_name(nm))
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


WIKI_TITLES = {
    "NVDA": "Nvidia", "AAPL": "Apple_Inc.", "MSFT": "Microsoft", "TSLA": "Tesla,_Inc.",
    "AMZN": "Amazon_(company)", "GOOGL": "Google", "META": "Meta_Platforms",
    "AMD": "Advanced_Micro_Devices", "NFLX": "Netflix", "PLTR": "Palantir_Technologies",
    "COIN": "Coinbase", "HOOD": "Robinhood_Markets", "GME": "GameStop", "AMC": "AMC_Theatres",
    "UBER": "Uber", "ABNB": "Airbnb", "DIS": "The_Walt_Disney_Company", "SHOP": "Shopify",
    "PYPL": "PayPal", "INTC": "Intel", "MU": "Micron_Technology", "QCOM": "Qualcomm",
    "ORCL": "Oracle_Corporation", "CRM": "Salesforce", "SMCI": "Supermicro", "ARM": "Arm_Holdings",
    "TSM": "TSMC", "ASML": "ASML_Holding", "MRNA": "Moderna", "PFE": "Pfizer",
    "LLY": "Eli_Lilly_and_Company", "BA": "Boeing", "LMT": "Lockheed_Martin",
    "F": "Ford_Motor_Company", "GM": "General_Motors", "RIVN": "Rivian", "NIO": "Nio_Inc.",
    "WMT": "Walmart", "COST": "Costco", "NKE": "Nike,_Inc.", "SBUX": "Starbucks", "KO": "Coca-Cola",
}


class WikiAttentionSource(Source):
    """Web-visitor proxy: a spike in a company's Wikipedia pageviews = surging public attention,
    which front-runs demand and earnings reactions. Free Wikimedia pageviews API (the closest free
    stand-in for the site-traffic data quant funds buy)."""
    key = "wiki_attention"
    min_interval = 21600.0       # pageviews are daily; check every 6h

    def fetch(self) -> list[RawSignal]:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=31)
        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        out: list[RawSignal] = []
        for tk, title in WIKI_TITLES.items():
            try:
                url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
                       f"en.wikipedia/all-access/user/{title}/daily/{s}/{e}")
                r = requests.get(url, headers={"User-Agent": "TENTACLE/1.0 (market research)"}, timeout=10)
                views = [it["views"] for it in r.json().get("items", [])]
                if len(views) < 8:
                    continue
                base = sum(views[:-1]) / max(1, len(views) - 1)
                latest = views[-1]
                if base > 0 and latest / base >= 2.0:           # 2x normal attention
                    ratio = latest / base
                    out.append(RawSignal(
                        source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                        headline=f"{tk} Wikipedia attention spike {ratio:.1f}x normal",
                        url="https://wikimedia.org", strength=min(2.0, 0.5 + ratio / 4)))
            except Exception:
                pass
        return out


class NasdaqHaltsSource(Source):
    """Live NYSE/Nasdaq trading halts + resumptions (free Nasdaq Trader feed). A news-pending halt
    or a fresh resumption is a real volatility catalyst — the stock is about to move on something.
    Volatility (LUDP) halts on micro-caps get filtered downstream by the liquidity floor."""
    key = "nasdaq_halts"
    min_interval = 90.0
    URL = "http://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
    NEWS_CODES = {"T1", "T2", "T6", "T8", "T12", "H10", "H4", "M", "MWC1", "MWC2", "MWC3"}

    @staticmethod
    def _tag(block: str, name: str) -> str:
        m = re.search(rf"<[^>]*{name}>([^<]*)<", block)
        return (m.group(1).strip() if m else "")

    def fetch(self) -> list[RawSignal]:
        try:
            r = requests.get(self.URL, headers={"User-Agent": "TENTACLE/1.0 (market research)"},
                             timeout=TIMEOUT)
        except Exception:
            return []
        out: list[RawSignal] = []
        for block in r.text.split("<item>")[1:]:
            sym = self._tag(block, "IssueSymbol")
            if not sym or not sym.isalpha() or len(sym) > 5:
                continue
            code = self._tag(block, "ReasonCode")
            resumed = bool(self._tag(block, "ResumptionTradeTime"))
            news_halt = code in self.NEWS_CODES
            if not (news_halt or resumed):
                continue                                  # skip routine non-news halts
            what = "resuming after" if resumed else "halted —"
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(), tickers=[sym],
                headline=f"{sym} {what} trading halt ({code or 'n/a'})",
                url="https://www.nasdaqtrader.com/Trader.aspx?id=TradeHalts",
                catalyst_hint=None, strength=1.0 if (news_halt and resumed) else 0.7))
        return out
