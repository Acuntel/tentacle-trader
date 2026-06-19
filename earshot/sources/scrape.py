"""HTML scraper tentacles — modeled on Brady's distressed-scraper pattern:
requests.Session + spoofed Chrome UA + randomized polite delays + per-target failure tolerance +
BeautifulSoup parse. For high-signal sites that DON'T offer a clean RSS/API.

Edge philosophy borrowed from his scraper: go to the EARLIEST public source and parse it
ourselves, rather than waiting for a downstream feed to surface it. See SCRAPER_NOTES.md.
"""
from __future__ import annotations

import random
import re
import time
import requests
from bs4 import BeautifulSoup

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")          # his exact UA
TICKER_HREF = re.compile(r"^/([A-Z]{1,5})$")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


class OpenInsiderSource(Source):
    """Scrape OpenInsider's latest CLUSTER BUYS table (several insiders buying the same name at once)
    — a strong whale signal aggregated for us, with no API. His table-scrape pattern."""
    key = "edgar_form4"          # insider family + weight bucket
    min_interval = 900.0
    URL = "http://openinsider.com/latest-cluster-buys"

    def fetch(self) -> list[RawSignal]:
        r = _session().get(self.URL, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", class_="tinytable")
        out: list[RawSignal] = []
        if not table:
            return out
        for tr in table.find_all("tr")[1:60]:
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            ticker = None
            for a in tr.find_all("a"):
                m = TICKER_HREF.match((a.get("href") or "").strip())
                if m:
                    ticker = m.group(1)
                    break
            if not ticker:
                continue
            txt = " ".join(td.get_text(strip=True) for td in tds)
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(), tickers=[ticker],
                headline=f"OpenInsider cluster buy: {ticker} — {txt[:90]}", url=self.URL,
                catalyst_hint="insider_cluster", strength=1.2,
            ))
        return out


class HtmlScraperSource(Source):
    """Generic config-driven scraper. Each target = {url, selector, src_key, strength}. Add a site
    by adding a CSS selector — no new code. Polite randomized delays (his anti-ban pattern)."""
    key = "google_news"
    min_interval = 300.0

    def __init__(self, weights: dict, targets: list):
        super().__init__(weights)
        self.targets = targets or []

    def fetch(self) -> list[RawSignal]:
        s = _session()
        out, seen = [], set()
        for t in self.targets:
            try:
                r = s.get(t["url"], timeout=20)
                soup = BeautifulSoup(r.text, "html.parser")
                for el in soup.select(t.get("selector", "a"))[:40]:
                    text = el.get_text(strip=True)
                    tickers = extract_tickers(text)
                    if not tickers or not text:
                        continue
                    k = (tuple(tickers), text[:80])
                    if k in seen:
                        continue
                    seen.add(k)
                    skey = t.get("src_key", self.key)
                    out.append(RawSignal(
                        source=skey, weight=self.weight(skey), ts=now_ts(), tickers=tickers,
                        headline=text[:160], url=el.get("href", t["url"]), strength=t.get("strength", 1.0)))
                time.sleep(random.uniform(0.5, 1.5))   # polite + randomized, like his scraper
            except Exception as e:
                print(f"    [scrape down] {t['url'][:45]}: {e}")
        return out
