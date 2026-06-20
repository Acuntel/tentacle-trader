"""MegaFleet — ~1000+ free 'tentacles'.

Generated from a ticker universe (S&P 500 pulled live + a hand-picked meme/quantum/biotech list)
x feed templates (Google News per ticker, Yahoo Finance per ticker), plus catalyst-keyword
searches, Reddit subs, and sector media. Polled in a ROTATING batch each tick (parallel) so we
sweep the whole fleet every ~15 min WITHOUT hammering any one domain — the anti-ban trick.

More volume alone isn't edge; the cross-family convergence brain + learning loop turn it into
signal. Adding tentacles = extending these lists.
"""
from __future__ import annotations

import concurrent.futures as cf
import urllib.parse
import requests
import feedparser

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers

UA = "Mozilla/5.0 (TENTACLE fleet)"

CORE_TICKERS = ("AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO BRK-B LLY JPM V WMT XOM UNH MA JNJ PG "
                "HD COST ORCL CVX MRK ABBV BAC KO PEP ADBE NFLX CRM AMD TMO LIN ACN MCD ABT CSCO "
                "DHR WFC TXN QCOM PM INTC INTU VZ CMCSA AMGN NKE UNP MS RTX NOW HON COP LOW SPGI "
                "BA CAT GS IBM").split()

EXTRA_TICKERS = ("IONQ RGTI QBTS QUBT SOUN BBAI PLTR SMCI MU ARM SOFI HOOD COIN MARA RIOT CLSK "
                 "MSTR GME AMC UBER LYFT ABNB DKNG RIVN LCID NIO XPEV LI CHPT PLUG ENPH FSLR NVAX "
                 "MRNA BNTX SAVA AXSM CRSP NTLA BEAM VKTX RXRX TEM HIMS OSCR DNA PATH AI GTLB NET "
                 "DDOG SNOW CRWD ZS PANW MDB OKTA TTD ROKU PINS SHOP SQ PYPL AFRM UPST TSM ASML "
                 "MRVL LRCX KLAC AMAT ON WOLF LMT NOC GD OXY "
                 # — expanded fleet —
                 "LHX LDOS BAH HII KTOS AVAV RKLB ASTS LUNR ACHR JOBY EH RCAT ONDS "
                 "GILD BIIB VRTX REGN BMY SRPT BLUE EXEL ALNY INCY MRTX IOVA CYTK DVAX KRYS "
                 "TSLA F GM FORD STLA TM HMC NKLA HYLN GOEV FFIE "
                 "CVNA CHWY ETSY W RH BBWI ELF DECK CROX ONON BIRK "
                 "DELL HPQ HPE WDC STX ANET CIEN JNPR FFIV NTAP PSTG "
                 "T VZ TMUS CMCSA CHTR WBD PARA FUBO RDDT SPOT DASH ZG "
                 "XOM CVX COP SLB HAL DVN FANG MPC PSX VLO KMI WMB ET LNG "
                 "FCX NEM AA CLF X NUE STLD MP ALB LAC SQM CCJ UEC URA UUUU "
                 "JPM BAC WFC C GS MS SCHW BLK KKR APO BX ALLY DFS SYF "
                 "ABNB MGM LVS WYNN CZR PENN RCL CCL NCLH UAL DAL AAL LUV "
                 "AMD INTC TSM ASML AMAT KLAC TER ENTG COHR INDI MBLY "
                 "CRSP NTLA BEAM PRME VERV EDIT NVCR TDOC DOCS GH NTRA "
                 "PLTR SNOW NET CFLT S DT ESTC FROG BRZE PD GTLB ASAN MNDY").split()

KEYWORDS = ["FDA approval", "phase 3 results", "to acquire", "merger agreement", "added to S&P 500",
            "raises full-year guidance", "earnings beat", "wins contract", "stock halted",
            "files for bankruptcy", "activist investor stake", "stock buyback", "reverse stock split",
            "FDA rejects", "clinical trial results", "government contract awarded", "analyst upgrade",
            "price target raised", "guides above", "strategic review",
            # — expanded catalysts —
            "definitive agreement", "to be acquired", "takeover bid", "going private", "tender offer",
            "PDUFA date", "breakthrough therapy", "fast track designation", "topline results",
            "DoD contract", "NASA awards", "Pentagon awards", "billion contract", "task order",
            "raises dividend", "initiates dividend", "special dividend", "secondary offering",
            "short squeeze", "13D filed", "stake in", "joins Nasdaq-100", "index reconstitution",
            "CEO resigns", "CFO departs", "guidance cut", "profit warning", "recalls",
            "data readout", "interim analysis", "primary endpoint met", "license agreement",
            "wins approval", "halts trading", "resumes trading", "all-time high", "record bookings"]

REDDIT = ["wallstreetbets", "stocks", "options", "investing", "StockMarket", "biotechstocks",
          "SPACs", "pennystocks", "Shortsqueeze", "ValueInvesting", "SecurityAnalysis",
          "smallstreetbets", "Biotechplays", "Vitards", "weedstocks", "EVStocks", "wallstreetbetsELITE",
          "trakstocks", "robinhoodpennystocks", "stockstobuytoday"]

MEDIA = [
    ("https://www.fiercebiotech.com/rss/xml", "newswire", 0.8),
    ("https://www.biopharmadive.com/feeds/news/", "newswire", 0.8),
    ("https://techcrunch.com/feed/", "google_news", 0.5),
    ("https://www.theverge.com/rss/index.xml", "google_news", 0.45),
    ("https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "google_news", 0.7),
    ("https://www.investing.com/rss/news.rss", "google_news", 0.6),
    ("https://www.nasdaq.com/feed/rssoutbound?category=Markets", "google_news", 0.6),
    ("https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20", "gov_contract", 0.8),
    ("https://seekingalpha.com/market_currents.xml", "google_news", 0.9),
    ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "google_news", 0.9),
    # — expanded media / wire feeds (all free RSS) —
    ("https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire", "newswire", 0.9),
    ("https://www.prnewswire.com/rss/news-releases-list.rss", "newswire", 0.85),
    ("https://feeds.content.dowjones.io/public/rss/mw_topstories", "google_news", 0.7),
    ("https://www.businesswire.com/portal/site/home/news/", "newswire", 0.8),
    ("https://www.fiercepharma.com/rss/xml", "newswire", 0.8),
    ("https://endpts.com/feed/", "newswire", 0.75),
    ("https://www.statnews.com/feed/", "google_news", 0.6),
    ("https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml", "fda", 0.9),
    ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom", "edgar_8k", 0.9),
    ("https://www.federalreserve.gov/feeds/press_all.xml", "google_news", 0.5),
    ("https://home.treasury.gov/system/files/126/ofac.xml", "google_news", 0.4),
    ("https://www.reuters.com/markets/us/rss", "google_news", 0.8),
    ("https://www.marketwatch.com/rss/topstories", "google_news", 0.7),
    ("https://www.benzinga.com/feed", "google_news", 0.7),
    ("https://finance.yahoo.com/news/rssindex", "google_news", 0.7),
    ("https://www.ainvest.com/rss", "google_news", 0.4),
]


def _gnews(q: str) -> str:
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"


def _yahoo(t: str) -> str:
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US"


def _bing(t: str) -> str:
    return f"https://www.bing.com/news/search?q=%22{t}%22+stock&format=rss"


def _sp500() -> list[str]:
    try:
        r = requests.get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
                         "main/data/constituents.csv", headers={"User-Agent": UA}, timeout=15)
        out = []
        for ln in r.text.splitlines()[1:]:
            sym = ln.split(",")[0].strip().replace(".", "-")
            if sym and sym.replace("-", "").isalpha():
                out.append(sym)
        return out
    except Exception:
        return []


def _all_us_symbols() -> list[str]:
    """Full US listings (NASDAQ + NYSE/other) from the free Nasdaq Trader symbol files — ~8k names."""
    syms: list[str] = []
    for url in ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            for ln in r.text.splitlines()[1:]:
                if ln.startswith("File Creation Time"):
                    continue
                sym = ln.split("|")[0].strip()
                if sym and sym.isalpha() and len(sym) <= 5:
                    syms.append(sym)
        except Exception:
            pass
    return syms


def build_feed_list(universe: str = "sp500", wide_cap: int = 20000) -> tuple[list, list]:
    """Returns (fast_feeds, wide_feeds). Fast = liquid core (swept often). Wide = full US long
    tail (swept slowly) — only when universe='all'. Tiering = 10k breadth without making the
    names that matter stale."""
    fast_tickers = list(dict.fromkeys((_sp500() or CORE_TICKERS) + EXTRA_TICKERS))
    fast: list[tuple] = []
    for t in fast_tickers:
        fast.append((_gnews(f'"{t}" stock'), "google_news", 0.5))
        fast.append((_yahoo(t), "google_news", 0.55))
        fast.append((_bing(t), "google_news", 0.5))                 # 3rd independent news angle/ticker
    fast += [(_gnews(q), "google_news", 0.5) for q in KEYWORDS]
    fast += [(f"https://www.reddit.com/r/{s}/.rss", "reddit", 0.35) for s in REDDIT]
    fast += MEDIA

    wide: list[tuple] = []
    if universe == "all":
        fastset = set(fast_tickers)
        for t in _all_us_symbols():
            if t in fastset:
                continue
            wide.append((_gnews(f'"{t}" stock'), "google_news", 0.4))
            wide.append((_yahoo(t), "google_news", 0.45))
            wide.append((_bing(t), "google_news", 0.4))
            if len(wide) >= wide_cap:
                break
    return fast, wide


class MegaFleetSource(Source):
    """Up to ~10k+ feeds in two tiers: a FAST liquid core (swept often) + a WIDE full-US long tail
    (swept slowly). Each tick polls a rotating batch from each tier in parallel — so the names that
    matter stay fresh while breadth scales, and no domain gets hammered."""
    key = "google_news"
    min_interval = 20.0

    def __init__(self, weights: dict, fast_batch: int = 25, wide_batch: int = 20,
                 universe: str = "sp500", wide_cap: int = 20000):
        super().__init__(weights)
        self.fast, self.wide = build_feed_list(universe, wide_cap)
        self.fb, self.wb = fast_batch, wide_batch
        self.fi = self.wi = 0
        total = len(self.fast) + len(self.wide)
        fsweep = len(self.fast) * self.min_interval / max(1, fast_batch) / 60
        wsweep = len(self.wide) * self.min_interval / max(1, wide_batch) / 60
        print(f"  [megafleet] {total} tentacles = {len(self.fast)} fast + {len(self.wide)} wide. "
              f"fast sweep ~{fsweep:.0f}m, wide sweep ~{wsweep:.0f}m")

    def _one(self, feed):
        url, skey, strength = feed
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=8)
            fp = feedparser.parse(r.content)
            out = []
            for e in fp.entries[:15]:
                tk = extract_tickers(f"{getattr(e, 'title', '')} {getattr(e, 'summary', '')}")
                if tk:
                    out.append(RawSignal(source=skey, weight=self.weight(skey), ts=now_ts(),
                                         tickers=tk, headline=getattr(e, "title", "")[:160],
                                         url=getattr(e, "link", ""), strength=strength))
            return out
        except Exception:
            return []

    @staticmethod
    def _rot(pool, idx, n):
        if not pool:
            return [], idx
        return [pool[(idx + i) % len(pool)] for i in range(min(n, len(pool)))], (idx + n) % len(pool)

    def fetch(self) -> list[RawSignal]:
        fb, self.fi = self._rot(self.fast, self.fi, self.fb)
        wb, self.wi = self._rot(self.wide, self.wi, self.wb)
        out, seen = [], set()
        with cf.ThreadPoolExecutor(max_workers=24) as ex:
            for res in ex.map(self._one, fb + wb):
                for sg in res:
                    k = (sg.source, tuple(sg.tickers), sg.headline)
                    if k not in seen:
                        seen.add(k)
                        out.append(sg)
        return out
