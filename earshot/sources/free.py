"""Keyless free tentacles — these run with ZERO API keys."""
from __future__ import annotations

import requests
import feedparser

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers

TIMEOUT = 12
AI_QUANTUM_KW = ("ai ", "a.i.", "gpt", "llm", "quantum", "chip", "gpu", "nvidia",
                 "openai", "anthropic", "semiconductor", "data center", "agentic")


class EdgarSource(Source):
    """SEC EDGAR real-time filings: 8-K (material events), Form 4 (insider buys), SC 13D (activist)."""
    key = "edgar_8k"
    FEEDS = {
        "8-K": "edgar_8k",
        "4": "edgar_form4",
        "SC 13D": "edgar_13d",
    }
    HINTS = {"edgar_8k": None, "edgar_form4": "insider_cluster", "edgar_13d": "activist_stake"}

    def __init__(self, weights: dict, user_agent: str):
        super().__init__(weights)
        self.ua = user_agent
        self._cik_map: dict[int, str] | None = None

    def _ciks(self) -> dict[int, str]:
        if self._cik_map is None:
            self._cik_map = {}
            try:
                r = requests.get("https://www.sec.gov/files/company_tickers.json",
                                 headers={"User-Agent": self.ua}, timeout=TIMEOUT)
                for row in r.json().values():
                    self._cik_map[int(row["cik_str"])] = row["ticker"]
            except Exception as e:
                print(f"  [tentacle:edgar] cik map failed: {e}")
        return self._cik_map

    def fetch(self) -> list[RawSignal]:
        out: list[RawSignal] = []
        ciks = self._ciks()
        for form, skey in self.FEEDS.items():
            url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
                   f"&type={form.replace(' ', '+')}&company=&dateb=&owner=include&count=40&output=atom")
            r = requests.get(url, headers={"User-Agent": self.ua}, timeout=TIMEOUT)
            feed = feedparser.parse(r.text)
            for e in feed.entries:
                title = getattr(e, "title", "")
                cik = None
                for tok in title.replace("(", " ").replace(")", " ").split():
                    if tok.isdigit() and len(tok) >= 7:
                        cik = int(tok)
                ticker = ciks.get(cik) if cik else None
                tickers = [ticker] if ticker else extract_tickers(title)
                if not tickers:
                    continue
                out.append(RawSignal(
                    source=skey, weight=self.weight(skey), ts=now_ts(),
                    tickers=tickers, headline=title, url=getattr(e, "link", ""),
                    catalyst_hint=self.HINTS[skey], strength=1.0,
                ))
        return out


class ApeWisdomSource(Source):
    """Free Reddit/StockTwits mention-velocity. Confirmation only (never a sole trigger)."""
    key = "apewisdom"

    def fetch(self) -> list[RawSignal]:
        r = requests.get("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1", timeout=TIMEOUT)
        out: list[RawSignal] = []
        for row in r.json().get("results", [])[:50]:
            tk = row.get("ticker")
            m, m0 = row.get("mentions", 0), max(row.get("mentions_24h_ago", 0), 1)
            vel = (m - m0) / m0
            if tk and vel > 1.5 and m >= 25:  # mentions >2.5x in 24h
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(),
                    tickers=[tk], headline=f"{tk} retail mentions {m} (+{vel*100:.0f}% 24h)",
                    url="https://apewisdom.io", catalyst_hint=None,
                    strength=min(2.0, 0.5 + vel / 4), raw=row,
                ))
        return out


class HackerNewsSource(Source):
    """HN front page = fast AI/quantum/tech radar. Low weight, theme-tagging."""
    key = "hackernews"

    def fetch(self) -> list[RawSignal]:
        r = requests.get("http://hn.algolia.com/api/v1/search_by_date?tags=front_page&hitsPerPage=40",
                         timeout=TIMEOUT)
        out: list[RawSignal] = []
        for h in r.json().get("hits", []):
            title = h.get("title") or ""
            low = title.lower()
            tickers = extract_tickers(title)
            is_theme = any(k in low for k in AI_QUANTUM_KW)
            if not tickers:
                continue  # need a tradeable ticker; pure-theme stories are logged but not traded
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(),
                tickers=tickers, headline=title, url=h.get("url") or "",
                catalyst_hint="ai_quantum_breakout" if is_theme else None,
                strength=min(2.0, 0.4 + (h.get("points", 0) / 300)),
            ))
        return out


class RssSource(Source):
    """Generic RSS tentacle — the X-around. Point it at RSSHub / Nitter / Telegram-RSS / newswire / Google News."""
    key = "google_news"

    def __init__(self, url: str, weights: dict):
        super().__init__(weights)
        self.url = url
        low = url.lower()
        self.key = ("x_relay" if ("twitter" in low or "nitter" in low) else
                    "telegram" if "telegram" in low else
                    "newswire" if ("globenewswire" in low or "businesswire" in low or "prnewswire" in low) else
                    "google_news")

    def fetch(self) -> list[RawSignal]:
        feed = feedparser.parse(self.url)
        out: list[RawSignal] = []
        for e in feed.entries[:40]:
            text = f"{getattr(e, 'title', '')} {getattr(e, 'summary', '')}"
            tickers = extract_tickers(text)
            if not tickers:
                continue
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(),
                tickers=tickers, headline=getattr(e, "title", ""),
                url=getattr(e, "link", ""), strength=1.0,
            ))
        return out
