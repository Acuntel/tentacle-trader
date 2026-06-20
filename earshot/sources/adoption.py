"""Package adoption tentacles — developer pull velocity as a dev-tool revenue proxy (free primary).

PyPI / npm / Docker Hub publish download counts for free. For a PURE-PLAY dev-tool public company,
an acceleration in its package's downloads front-runs usage-based revenue. Baseline-delta: persist the
last reading per package, emit ONLY on a real growth jump (degrade loudly). Mega-cap packages (boto3,
azure) are excluded — the signal is swamped by the parent; we track the names where adoption moves the
stock. Family 'demand'.
"""
from __future__ import annotations

import requests

from ..models import RawSignal, now_ts
from .base import Source
from .altdata import _load, _save

TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (TENTACLE adoption research)"}

# pure-play dev-tool packages where downloads actually move the stock (package -> ticker)
PYPI = {"snowflake-connector-python": "SNOW", "datadog": "DDOG", "elasticsearch": "ESTC",
        "pymongo": "MDB", "confluent-kafka": "CFLT", "twilio": "TWLO", "simple-salesforce": "CRM",
        "hubspot-api-client": "HUBS", "cloudflare": "NET", "atlassian-python-api": "TEAM"}
NPM = {"@datadog/browser-rum": "DDOG", "@elastic/elasticsearch": "ESTC", "mongodb": "MDB",
       "twilio": "TWLO", "snowflake-sdk": "SNOW", "@shopify/shopify-api": "SHOP"}
DOCKER = {"library/elasticsearch": "ESTC", "library/mongo": "MDB", "confluentinc/cp-kafka": "CFLT",
          "datadog/agent": "DDOG"}


def _emit(key, weight, tk, label, old, new, growth):
    return RawSignal(source=key, weight=weight, ts=now_ts(), tickers=[tk],
                     headline=f"{tk} dev adoption rising — {label} {old:,}→{new:,} (+{growth:.0%})",
                     url="https://pypistats.org", strength=min(2.0, 0.6 + growth * 3))


class PyPiDownloadsSource(Source):
    key = "pypi"
    min_interval = 21600.0          # PyPI stats update daily; 6h cadence
    PATH = "state/adopt_pypi.json"

    def __init__(self, weights, batch=4):
        super().__init__(weights)
        self.pkgs = list(PYPI.keys()); self.i = 0; self.batch = batch

    def fetch(self):
        prev = _load(self.PATH, {})
        out = []
        picks = [self.pkgs[(self.i + k) % len(self.pkgs)] for k in range(self.batch)]
        self.i = (self.i + self.batch) % len(self.pkgs)
        for pkg in picks:
            try:
                r = requests.get(f"https://pypistats.org/api/packages/{pkg}/recent",
                                 headers=UA, timeout=TIMEOUT)
                n = int(r.json()["data"].get("last_week") or 0)
            except Exception:
                continue
            if not n:
                continue
            old = prev.get(pkg)
            if old and n > old * 1.15 and n - old >= 5000:
                out.append(_emit(self.key, self.weight(), PYPI[pkg], f"PyPI {pkg}/wk", old, n, n / old - 1))
            prev[pkg] = n
        _save(self.PATH, prev)
        return out


class NpmDownloadsSource(Source):
    key = "npm"
    min_interval = 21600.0
    PATH = "state/adopt_npm.json"

    def __init__(self, weights, batch=3):
        super().__init__(weights)
        self.pkgs = list(NPM.keys()); self.i = 0; self.batch = batch

    def fetch(self):
        prev = _load(self.PATH, {})
        out = []
        picks = [self.pkgs[(self.i + k) % len(self.pkgs)] for k in range(self.batch)]
        self.i = (self.i + self.batch) % len(self.pkgs)
        for pkg in picks:
            try:
                r = requests.get(f"https://api.npmjs.org/downloads/point/last-week/{pkg}",
                                 headers=UA, timeout=TIMEOUT)
                n = int(r.json().get("downloads") or 0)
            except Exception:
                continue
            if not n:
                continue
            old = prev.get(pkg)
            if old and n > old * 1.15 and n - old >= 10000:
                out.append(_emit(self.key, self.weight(), NPM[pkg], f"npm {pkg}/wk", old, n, n / old - 1))
            prev[pkg] = n
        _save(self.PATH, prev)
        return out


class DockerPullsSource(Source):
    key = "docker"
    min_interval = 21600.0
    PATH = "state/adopt_docker.json"

    def __init__(self, weights, batch=2):
        super().__init__(weights)
        self.repos = list(DOCKER.keys()); self.i = 0; self.batch = batch

    def fetch(self):
        prev = _load(self.PATH, {})
        out = []
        picks = [self.repos[(self.i + k) % len(self.repos)] for k in range(self.batch)]
        self.i = (self.i + self.batch) % len(self.repos)
        for repo in picks:
            try:
                r = requests.get(f"https://hub.docker.com/v2/repositories/{repo}/",
                                 headers=UA, timeout=TIMEOUT)
                n = int(r.json().get("pull_count") or 0)
            except Exception:
                continue
            if not n:
                continue
            old = prev.get(repo)
            if old and n - old >= 2_000_000:                  # cumulative -> a real pull-rate surge
                out.append(_emit(self.key, self.weight(), DOCKER[repo], f"Docker {repo}", old, n,
                                 (n - old) / max(old, 1)))
            prev[repo] = n
        _save(self.PATH, prev)
        return out
