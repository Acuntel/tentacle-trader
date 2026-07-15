"""Keyless free tentacles — these run with ZERO API keys."""
from __future__ import annotations

import json
import os
import re
import time

import requests
import feedparser

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers

TIMEOUT = 12
AI_QUANTUM_KW = ("ai ", "a.i.", "gpt", "llm", "quantum", "chip", "gpu", "nvidia",
                 "openai", "anthropic", "semiconductor", "data center", "agentic")


class EdgarSource(Source):
    """SEC EDGAR real-time filings: 8-K (material events), Form 4 (insider filings), SC 13D (activist)."""
    key = "edgar_8k"
    min_interval = 30.0          # filings can break anytime; ~12 req/30s is well under SEC's 10/s
    FEEDS = {
        "8-K": "edgar_8k",
        "4": "edgar_form4",
        "SC 13D": "edgar_13d",
    }
    # AUDIT FIX: raw Form 4s are NOT bullish insider clusters — the majority are sells/option
    # exercises, and stamping the whole firehose "insider_cluster" made it the worst-performing
    # family (2566 trades, 40% win). Emit Form 4s unhinted (generic filing evidence -> keyword
    # classify -> UNKNOWN); the real cluster-BUY signal is OpenInsider in scrape.py.
    HINTS = {"edgar_8k": None, "edgar_form4": None, "edgar_13d": "activist_stake"}
    # ...but a Form 4 that IS an open-market purchase (code P) is genuinely bullish and early.
    # Re-earn the hint by reading the filing's XML: bounded per poll, verdicts cached so re-polled
    # entries never re-fetch, and any failure just emits unhinted exactly like the line above.
    FORM4_XML_BUDGET = 10        # max filing fetches per poll (the feed yields ~40 entries)
    FORM4_FETCH_DELAY = 0.3      # gap between filing fetches — far under SEC's ~10 req/s fair-use
    FORM4_CACHE_CAP = 2000
    ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
    NONDERIV_TXN_RE = re.compile(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
                                 re.S | re.I)
    CODE_P_RE = re.compile(r"<transactionCode>\s*P\s*</transactionCode>", re.I)
    ACQUIRED_RE = re.compile(r"<transactionAcquiredDisposedCode>\s*<value>\s*A\s*</value>",
                             re.S | re.I)

    def __init__(self, weights: dict, user_agent: str):
        super().__init__(weights)
        self.ua = user_agent
        self._cik_map: dict[int, str] | None = None
        self._form4_path = os.path.join("state", "form4_verdicts.json")
        try:
            with open(self._form4_path) as f:
                self._form4_verdicts = {str(k): bool(v) for k, v in json.load(f).items()}
        except Exception:
            self._form4_verdicts = {}
        self._form4_budget = 0

    def _save_form4_verdicts(self):
        try:
            if len(self._form4_verdicts) > self.FORM4_CACHE_CAP:  # insertion-ordered dict -> FIFO cap
                for k in list(self._form4_verdicts)[: len(self._form4_verdicts) - self.FORM4_CACHE_CAP]:
                    del self._form4_verdicts[k]
            os.makedirs("state", exist_ok=True)
            with open(self._form4_path, "w") as f:
                json.dump(self._form4_verdicts, f)
        except Exception:
            pass

    def _form4_is_purchase(self, link: str) -> bool:
        """True ONLY for a verified open-market buy: the filing has a non-derivative transaction
        coded P and acquired ("A"). Sells, option exercises, grants, over-budget entries and
        anything we can't fetch/parse -> False (signal still emitted, unhinted as today)."""
        m = self.ACCESSION_RE.search(link or "")
        if not m or "/" not in link:
            return False
        acc = m.group(0)
        if acc in self._form4_verdicts:                    # re-polled entries never re-fetch
            return self._form4_verdicts[acc]
        if self._form4_budget <= 0:                        # bounded; uncached -> retried next poll
            return False
        self._form4_budget -= 1
        try:
            time.sleep(self.FORM4_FETCH_DELAY)             # politeness gap between filing fetches
            url = f"{link.rsplit('/', 1)[0]}/{acc}.txt"    # full submission embeds the ownership XML
            r = requests.get(url, headers={"User-Agent": self.ua}, timeout=TIMEOUT)
            r.raise_for_status()
            verdict = any(self.CODE_P_RE.search(txn) and self.ACQUIRED_RE.search(txn)
                          for txn in self.NONDERIV_TXN_RE.findall(r.text))
        except Exception as e:                             # graceful: failures aren't cached
            print(f"  [tentacle:edgar] form4 xml {acc} failed (emit unhinted): {e}")
            return False
        self._form4_verdicts[acc] = verdict
        self._save_form4_verdicts()
        return verdict

    def _ciks(self) -> dict[int, str]:
        # AUDIT FIX: retry when the map is EMPTY, not only when None — one failed fetch at boot used to
        # cache {} forever, permanently degrading EDGAR ticker extraction (the bot's top-trust source).
        if not self._cik_map:
            m: dict[int, str] = {}
            try:
                r = requests.get("https://www.sec.gov/files/company_tickers.json",
                                 headers={"User-Agent": self.ua}, timeout=TIMEOUT)
                for row in r.json().values():
                    m[int(row["cik_str"])] = row["ticker"]
            except Exception as e:
                print(f"  [tentacle:edgar] cik map failed (will retry next poll): {e}")
            self._cik_map = m or None                    # None -> retried on the next fetch()
        return self._cik_map or {}

    def fetch(self) -> list[RawSignal]:
        out: list[RawSignal] = []
        ciks = self._ciks()
        self._form4_budget = self.FORM4_XML_BUDGET
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
                link = getattr(e, "link", "")
                hint = self.HINTS[skey]
                if skey == "edgar_form4" and self._form4_is_purchase(link):
                    hint = "insider_cluster"       # re-earned: verified code-P open-market buy
                out.append(RawSignal(
                    source=skey, weight=self.weight(skey), ts=now_ts(),
                    tickers=tickers, headline=title, url=link,
                    catalyst_hint=hint, strength=1.0,
                ))
        return out


class ApeWisdomSource(Source):
    """Free Reddit/StockTwits mention-velocity. Confirmation only (never a sole trigger)."""
    key = "apewisdom"
    min_interval = 600.0         # mention counts update ~hourly; no point polling faster

    def fetch(self) -> list[RawSignal]:
        r = requests.get("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1", timeout=TIMEOUT)
        out: list[RawSignal] = []
        for row in r.json().get("results", [])[:50]:
            tk = row.get("ticker")
            m, m0 = (row.get("mentions") or 0), max(row.get("mentions_24h_ago") or 0, 1)
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
    min_interval = 90.0

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
    min_interval = 45.0

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
