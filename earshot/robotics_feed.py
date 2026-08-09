"""24/7 collector for the ROBOTICS SCOUT desk — sweeps the whole robotics universe around the
clock into a corpus the daily synthesis reads. RESEARCH ONLY: nothing here emits trading signals.

Universe = the scored names + humanoid OEM programs + adjacent public names + the Chinese chain +
private chokepoints + upcoming listings + bottleneck themes. Rotating small batches (anti-ban,
same discipline as the MegaFleet): a handful of Google News RSS queries per tick, an hourly SEC
EDGAR full-text sweep, and a bounded number of full-article reads per tick for the items that
matter. Everything lands in state/robotics/corpus.jsonl with a source tier and dedupe."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from datetime import date, timedelta

import feedparser
import requests

from .persist import load_json, save_json

CORPUS = "state/robotics/corpus.jsonl"
SEEN_PATH = "state/robotics/seen.json"
PENDING_SEND = "state/robotics/pending_send.json"

# (tag, google-news query). Tags matching a scored ticker get article-body reads.
UNIVERSE: list[tuple[str, str]] = [
    # scored names (tags = tickers, must match SEED_BOARD)
    ("MP", '"MP Materials"'), ("RRX", '"Regal Rexnord"'), ("TKR", '"Timken" robotics OR bearings'),
    ("NOVT", '"Novanta"'), ("AME", '"AMETEK"'), ("6481.T", '"THK" robot OR screw'),
    ("MOG-A", '"Moog" actuation OR robot'), ("688017.SS", '"Leaderdrive"'), ("ALNT", '"Allient"'),
    ("6324.T", '"Harmonic Drive"'), ("RBC", '"RBC Bearings"'), ("6268.T", '"Nabtesco"'),
    # humanoid OEM programs (the demand side of the whole thesis)
    ("optimus", "Tesla Optimus production OR ramp OR units"), ("figure", '"Figure AI" OR "Figure robot"'),
    ("unitree", "Unitree humanoid OR IPO"), ("agility", '"Agility Robotics" OR "Digit robot"'),
    ("apptronik", "Apptronik"), ("bostondyn", '"Boston Dynamics" Atlas'),
    ("1x", '"1X Technologies" OR "1X robot"'), ("ubtech", "UBTech humanoid"),
    ("xpeng-iron", "XPeng IRON humanoid"), ("fourier", '"Fourier Intelligence" robot'),
    ("mobis", "Hyundai Mobis actuator OR robot"), ("lg-axium", "LG Axium actuator"),
    # adjacent public names (near the industry)
    ("NVDA", "NVIDIA robotics OR Jetson OR GR00T"), ("QCOM", "Qualcomm Dragonwing robot"),
    ("TER", '"Universal Robots" OR Teradyne robotics'), ("ROK", '"Rockwell Automation"'),
    ("ABBNY", "ABB robotics"), ("6954.T", "Fanuc robot"), ("6506.T", "Yaskawa robot"),
    ("6861.T", "Keyence"), ("CGNX", "Cognex machine vision"), ("SYM", "Symbotic warehouse robot"),
    ("ISRG", '"Intuitive Surgical"'), ("ESTUN", "Estun robot"), ("INOVANCE", "Inovance humanoid OR servo"),
    # Chinese component chain (tracked for signal, not buyable)
    ("sanhua", "Sanhua humanoid OR actuator"), ("tuopu", "Tuopu humanoid OR actuator"),
    ("shuanghuan", "Shuanghuan gear humanoid"), ("hengli", "Hengli roller screw"),
    ("zhenyu", "Zhenyu actuator OR screw"),
    # private chokepoints (an acquisition or IPO of any of these is a thesis tell)
    ("rollvis", "Rollvis"), ("drake", '"Drake Manufacturing"'), ("holroyd", '"PTG Holroyd"'),
    ("reishauer", "Reishauer"), ("maxon", "Maxon motor robot"), ("faulhaber", "Faulhaber motor"),
    ("schunk", "Schunk gripper OR robot"), ("emag", "EMAG grinding"),
    # upcoming listings + themes (the bottleneck watch, 24/7 instead of Thursdays only)
    ("ipo", "AgiBot OR EngineAI OR Tesollo IPO robot"), ("screws", '"planetary roller screw"'),
    ("magnets", "NdPr OR rare earth magnet export"), ("reducers", "harmonic reducer humanoid"),
    ("grinders", "thread grinding machine capacity"), ("actuators", "humanoid actuator orders OR supplier"),
]

EDGAR_QUERIES = ['"humanoid"', '"humanoid robot"', '"robotics orders"', '"planetary roller screw"',
                 '"rare earth magnet"']

TIER1 = ("sec.gov", "globenewswire", "prnewswire", "businesswire", "accesswire", "stocktitan")
TIER2 = ("reuters", "barrons", "wsj", "bloomberg", "cnbc", "ft.com", "nikkei", "scmp", "investing.com",
         "benzinga", "marketwatch", "yahoo", "roboticsandautomationnews", "therobotreport", "techcrunch",
         "spectrum.ieee", "gasgoo", "globe")


def _tier(src: str) -> int:
    s = (src or "").lower()
    if any(d in s for d in TIER1):
        return 1
    if any(d in s for d in TIER2):
        return 2
    return 3


def _gnews(q: str) -> str:
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"


class RoboticsFeed:
    """Rotating always-on sweep. tick() is called from the main loop and must stay cheap."""

    def __init__(self, cfg: dict, ua: str):
        c = cfg.get("robotics", {}).get("collector", {})
        self.ua = ua
        self.poll_sec = int(c.get("poll_min", 5)) * 60
        self.batch = int(c.get("batch", 6))
        self.bodies_per_tick = int(c.get("bodies_per_tick", 2))
        self.corpus_hours = int(c.get("corpus_hours", 36))
        self.i = 0
        self.last_tick = 0.0
        self.last_edgar = 0.0
        self.edgar_i = 0
        self.seen: list = load_json(SEEN_PATH, [])
        self._seen_set = set(self.seen)
        self.collected_today = 0

    def due(self) -> bool:
        return time.time() - self.last_tick >= self.poll_sec

    def _remember(self, key: str) -> bool:
        """True if new. Persists a capped rolling window of seen keys."""
        if key in self._seen_set:
            return False
        self._seen_set.add(key)
        self.seen.append(key)
        if len(self.seen) > 8000:
            drop = self.seen[:2000]
            self.seen = self.seen[2000:]
            self._seen_set -= set(drop)
        return True

    def _append(self, item: dict):
        os.makedirs(os.path.dirname(CORPUS), exist_ok=True)
        with open(CORPUS, "a") as f:
            f.write(json.dumps(item) + "\n")
        self.collected_today += 1

    def _resolve_gnews(self, link: str) -> str:
        """Google News RSS links hide the publisher URL behind a JS redirect. Fast path: HTTP
        redirects. Slow path: the batchexecute decode. Returns '' when neither works — the item
        then just stays headline-only, never an error."""
        try:
            r = requests.get(link, headers={"User-Agent": self.ua}, timeout=8, allow_redirects=True)
            if "news.google.com" not in r.url:
                return r.url
            sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
            ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
            art_id = link.split("/articles/")[1].split("?")[0]
            if not (sg and ts and art_id):
                return ""
            payload = ('[[["Fbv4je","[\\"garturlreq\\",[[\\"en-US\\",\\"US\\",[\\"FINANCE_TOP_INDICES\\"],'
                       'null,null,1,1,\\"US:en\\",null,180,null,null,null,null,null,0,null,null,null],'
                       '\\"en-US\\",\\"US\\",1,[2,3,4,8],1,0,\\"0\\",0,0,null,0],\\"' + art_id + '\\",'
                       + ts.group(1) + ',\\"' + sg.group(1) + '\\"]",null,"generic"]]]')
            r2 = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                               headers={"User-Agent": self.ua,
                                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                               data={"f.req": payload}, timeout=8)
            m = re.search(r'https?://(?!news\.google)[^"\\\s]+', r2.text)
            return m.group(0) if m else ""
        except Exception:
            return ""

    def _body(self, link: str) -> str:
        """Best-effort full-text read of an article or SEC filing."""
        try:
            if "news.google.com" in link:
                link = self._resolve_gnews(link)
            if not link:
                return ""
            ua = "TENTACLE research bradyschwartz25@gmail.com" if "sec.gov" in link else self.ua
            r = requests.get(link, headers={"User-Agent": ua}, timeout=8, allow_redirects=True)
            if r.status_code != 200:
                return ""
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            txt = re.sub(r"\s+", " ", " ".join(p for p in paras if len(p) > 60))
            if len(txt) < 200:                     # sparse markup (some SEC exhibits) -> whole text
                txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
            return txt[:1500]
        except Exception:
            return ""

    def tick(self):
        self.last_tick = time.time()
        batch = [UNIVERSE[(self.i + k) % len(UNIVERSE)] for k in range(self.batch)]
        self.i = (self.i + self.batch) % len(UNIVERSE)
        bodies_left, fresh = self.bodies_per_tick, 0
        scored = {"MP", "RRX", "TKR", "NOVT", "AME", "6481.T", "MOG-A", "688017.SS", "ALNT",
                  "6324.T", "RBC", "6268.T"}
        for tag, q in batch:
            try:
                r = requests.get(_gnews(q), headers={"User-Agent": self.ua}, timeout=8)
                if r.status_code != 200:
                    continue
                for e in feedparser.parse(r.content).entries[:6]:
                    title = getattr(e, "title", "")[:170]
                    key = title.lower()[:80]
                    ts = time.mktime(e.published_parsed) if getattr(e, "published_parsed", None) else time.time()
                    if ts < time.time() - 3 * 86400 or not self._remember(key):
                        continue
                    src = title.rsplit(" - ", 1)[-1] if " - " in title else ""
                    item = {"ts": round(ts), "tag": tag, "title": title, "src": src, "tier": _tier(src),
                            "link": getattr(e, "link", "")}
                    if tag in scored and item["tier"] <= 2 and bodies_left > 0:
                        body = self._body(item["link"])
                        if body:
                            item["body"] = body
                            bodies_left -= 1
                    self._append(item)
                    fresh += 1
            except Exception:
                continue
        if time.time() - self.last_edgar >= 3600:
            fresh += self._edgar()
        save_json(SEEN_PATH, self.seen)
        if self.i < self.batch:                    # index wrapped -> one bound-check per full sweep
            self.rotate_corpus()
        return fresh

    def _edgar(self) -> int:
        """Hourly SEC full-text sweep: filings are the ground truth for 'real dollars today'."""
        self.last_edgar = time.time()
        q = EDGAR_QUERIES[self.edgar_i % len(EDGAR_QUERIES)]
        self.edgar_i += 1
        fresh = 0
        try:
            cutoff = (date.today() - timedelta(days=5)).isoformat()
            r = requests.get("https://efts.sec.gov/LATEST/search-index",
                             params={"q": q, "forms": "8-K,10-Q,10-K,6-K,S-1,DEF 14A",
                                     "dateRange": "custom", "startdt": cutoff,
                                     "enddt": date.today().isoformat()},
                             headers={"User-Agent": self.ua}, timeout=10)
            bodies_left = 2
            for h in (r.json().get("hits", {}) or {}).get("hits", [])[:10]:
                src = h.get("_source", {})
                names = " ".join(src.get("display_names", []) or [])[:90]
                hid = "edgar|" + (h.get("_id") or f"{src.get('file_date', '')}|{q}|{names}")
                if not self._remember(hid):
                    continue
                link = ""
                try:                               # _id = "adsh:filename" -> direct sec.gov doc URL
                    adsh, fname = (h.get("_id") or ":").split(":", 1)
                    cik = (src.get("ciks") or ["0"])[0].lstrip("0")
                    if adsh and fname and cik:
                        link = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                                f"{adsh.replace('-', '')}/{fname}")
                except Exception:
                    pass
                item = {"ts": round(time.time()), "tag": "edgar", "tier": 1, "src": "sec.gov",
                        "title": f"SEC {src.get('form', '?')} matches {q}: {names} "
                                 f"({src.get('file_date', '?')})", "link": link}
                if link and bodies_left > 0:
                    body = self._body(link)
                    if body:
                        item["body"] = body
                        bodies_left -= 1
                self._append(item)
                fresh += 1
        except Exception:
            pass
        return fresh

    def recent(self, hours: int | None = None) -> list[dict]:
        """Corpus tail for the daily synthesis (and anything else read-only)."""
        cutoff = time.time() - (hours or self.corpus_hours) * 3600
        out = []
        try:
            size = os.path.getsize(CORPUS)
            with open(CORPUS) as f:
                if size > 800_000:
                    f.seek(size - 800_000)
                    f.readline()
                for line in f:
                    try:
                        it = json.loads(line)
                        if it.get("ts", 0) >= cutoff:
                            out.append(it)
                    except Exception:
                        continue
        except Exception:
            return []
        return out

    def rotate_corpus(self):
        """Bound the corpus file; the volume is shared with the trader's decade of bars."""
        try:
            if os.path.getsize(CORPUS) > 5_000_000:
                with open(CORPUS) as f:
                    f.seek(os.path.getsize(CORPUS) - 2_000_000)
                    f.readline()
                    keep = f.read()
                with open(CORPUS, "w") as f:
                    f.write(keep)
        except Exception:
            pass


def queue_send(text: str):
    save_json(PENDING_SEND, {"text": text, "tries": 0, "last": 0.0})


def _drop_pending():
    """Remove the queue AND its .bak — load_json falls back to the .bak, so leaving it behind
    would resurrect an already-delivered message every 2 minutes."""
    for p in (PENDING_SEND, PENDING_SEND + ".bak"):
        try:
            os.remove(p)
        except Exception:
            pass


def flush_pending(send_fn) -> bool:
    """Retry a failed Telegram send every 2 minutes until delivered (cap 10 tries). Called from
    the main loop every cycle; a missing pending file makes this a no-op."""
    p = load_json(PENDING_SEND, None)
    if not p:
        return False
    if time.time() - p.get("last", 0) < 120:
        return False
    if send_fn(p["text"]):
        _drop_pending()
        return True
    p["tries"] = p.get("tries", 0) + 1
    p["last"] = time.time()
    if p["tries"] >= 10:
        _drop_pending()
        print("  [robotics] Telegram send gave up after 10 tries — message dropped")
        return False
    save_json(PENDING_SEND, p)
    return False
