"""Demand / usage tentacles — "how many people are buying & using the product", FREE primary sources.

These collect the thin version of what Sensor Tower / Apptopia / Similarweb resell: official, public,
no-auth feeds. Each tentacle is BASELINE-DELTA — it persists the last reading per entity and emits a
signal ONLY on a real move (a rank climb, a player surge, a star spike), never a stale repeat dressed
as fresh (master-prompt rule: degrade loudly). Entity resolution maps every record to a ticker; the
signal's ts IS its knowability time (we emit when we observe, so there is no lookahead). New family =
'demand' → an orthogonal confirmation pillar for the analyst brain.
"""
from __future__ import annotations

import json
import os

import requests

from ..models import RawSignal, now_ts
from .base import Source

TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (TENTACLE demand research)"}


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception:
        pass


# ---- App Store rankings: a tracked app climbing the chart = rising consumer demand ----
APP_NAME_TICKER = {
    "robinhood": "HOOD", "coinbase": "COIN", "doordash": "DASH", "uber": "UBER", "lyft": "LYFT",
    "spotify": "SPOT", "airbnb": "ABNB", "pinterest": "PINS", "snapchat": "SNAP", "roblox": "RBLX",
    "draftkings": "DKNG", "cash app": "XYZ", "block": "XYZ", "paypal": "PYPL", "venmo": "PYPL",
    "reddit": "RDDT", "duolingo": "DUOL", "netflix": "NFLX", "disney+": "DIS", "instagram": "META",
    "whatsapp": "META", "facebook": "META", "temu": "PDD", "amazon": "AMZN", "ebay": "EBAY",
    "etsy": "ETSY", "chewy": "CHWY", "carvana": "CVNA", "affirm": "AFRM", "sofi": "SOFI",
    "toast": "TOST", "peloton": "PTON", "tinder": "MTCH", "hinge": "MTCH", "bumble": "BMBL",
    "opendoor": "OPEN", "shop": "SHOP", "webull": "BULL",
}


class AppStoreDemandSource(Source):
    """Apple's official top-free / top-grossing RSS. A ticker's app climbing = demand accelerating."""
    key = "appstore"
    min_interval = 1800.0
    PATH = "state/demand_appstore.json"
    FEEDS = ("topfreeapplications", "topgrossingapplications")

    def _match(self, name: str):
        low = (name or "").lower()
        for k, tk in APP_NAME_TICKER.items():
            if k in low:
                return tk
        return None

    def fetch(self) -> list[RawSignal]:
        prev = _load(self.PATH, {})
        ranks: dict[str, int] = {}
        for feed in self.FEEDS:
            try:
                r = requests.get(f"https://itunes.apple.com/us/rss/{feed}/limit=100/json",
                                 headers=UA, timeout=TIMEOUT)
                entries = r.json()["feed"].get("entry", [])
            except Exception:
                continue
            for i, e in enumerate(entries):
                tk = self._match(e.get("im:name", {}).get("label", ""))
                if tk and (tk not in ranks or i + 1 < ranks[tk]):
                    ranks[tk] = i + 1                        # best (lowest) rank across both charts
        out: list[RawSignal] = []
        for tk, rank in ranks.items():
            old = prev.get(tk)
            climb = (old - rank) if old else 0
            if (old and climb >= 8) or (not old and rank <= 25):   # real move, not noise
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{tk} app demand rising — App Store rank {old or '—'}→#{rank}",
                    url="https://apps.apple.com", strength=min(2.0, 0.6 + max(climb, 25 - rank) / 30)))
        # AUDIT FIX: MERGE into the baseline like the steam/github siblings — writing the rebuilt dict
        # wiped every baseline whenever a fetch failed (both feeds erroring saved {}), and each name
        # then re-fired as a fresh "rank<=25" discovery on the next good poll.
        if ranks:
            prev.update(ranks)
            _save(self.PATH, prev)
        return out


# ---- Steam live concurrent players: a game surging = the publisher's product is being used NOW ----
STEAM_GAMES = {
    "271590": ("TTWO", "GTA V"), "1174180": ("TTWO", "Red Dead Redemption 2"),
    "1172470": ("EA", "Apex Legends"), "1222670": ("EA", "The Sims 4"),
    "2195250": ("EA", "EA SPORTS FC 24"), "1599340": ("AMZN", "Lost Ark"),
}


class SteamDemandSource(Source):
    """Steam GetNumberOfCurrentPlayers (official, no key). A player surge front-runs game revenue."""
    key = "steam"
    min_interval = 900.0
    PATH = "state/demand_steam.json"
    API = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={}"

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.ids = list(STEAM_GAMES.keys())
        self.i = 0

    def fetch(self) -> list[RawSignal]:
        prev = _load(self.PATH, {})
        out: list[RawSignal] = []
        picks = [self.ids[(self.i + k) % len(self.ids)] for k in range(min(4, len(self.ids)))]
        self.i = (self.i + 4) % len(self.ids)
        for appid in picks:
            try:
                r = requests.get(self.API.format(appid), headers=UA, timeout=TIMEOUT)
                n = int(r.json().get("response", {}).get("player_count") or 0)
            except Exception:
                continue
            if n <= 0:
                continue
            tk, name = STEAM_GAMES[appid]
            old = prev.get(appid)
            if old and n >= old * 1.3 and n - old >= 3000:      # a genuine usage surge
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{tk} usage surge — {name} concurrent players {old:,}→{n:,} on Steam",
                    url="https://steamdb.info", strength=min(2.0, 0.6 + (n / max(old, 1) - 1))))
            prev[appid] = n
        _save(self.PATH, prev)
        return out


# ---- GitHub star velocity: developer adoption proxy for dev-tool / cloud public companies ----
GITHUB_REPOS = {"mongodb/mongo": "MDB", "elastic/elasticsearch": "ESTC",
                "cloudflare/workerd": "NET", "confluentinc/librdkafka": "CFLT"}


class GithubActivitySource(Source):
    """Star velocity on a public company's flagship OSS repo — slow, weak, but a real adoption proxy."""
    key = "github"
    min_interval = 3600.0
    PATH = "state/demand_github.json"

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.repos = list(GITHUB_REPOS.keys())
        self.i = 0

    def fetch(self) -> list[RawSignal]:
        prev = _load(self.PATH, {})
        out: list[RawSignal] = []
        picks = [self.repos[(self.i + k) % len(self.repos)] for k in range(min(2, len(self.repos)))]
        self.i = (self.i + 2) % len(self.repos)
        for repo in picks:
            try:
                r = requests.get(f"https://api.github.com/repos/{repo}", headers=UA, timeout=TIMEOUT)
                stars = int(r.json().get("stargazers_count") or 0)
            except Exception:
                continue
            if not stars:
                continue
            tk = GITHUB_REPOS[repo]
            old = prev.get(repo)
            if old and stars - old >= 150:                      # a clear adoption jump (slow signal)
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{tk} developer adoption — {repo} stars {old:,}→{stars:,}",
                    url=f"https://github.com/{repo}", strength=min(1.5, 0.5 + (stars - old) / 500)))
            prev[repo] = stars
        _save(self.PATH, prev)
        return out
