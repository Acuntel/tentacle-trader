"""Shared name->ticker resolver — the SEC's own ~12k-issuer map instead of hand-typed tables.

WHY: base.py's NAME_TICKER (~70 names) and extra.py's CONTRACTOR/PHARMA tables (~30) were the
only way to turn a company NAME into a ticker, so the name-keyed tentacles resolved almost
nothing — UsaSpending could ticker-map only 15 of the top-40 weekly federal award recipients.
Meanwhile free.py already downloads company_tickers.json for its CIK->ticker map and throws
away the `title` field (the full legal name of every SEC issuer). This module keeps it:
normalized legal title -> ticker, disk-cached to state/name_ticker_map.json for ~7 days.

Used as the FALLBACK after the hand maps (they stay first — they encode aliases like
"lockheed" that a legal-title map can't know). Every guard prefers returning None over a
wrong ticker, because a match here feeds a trading signal:
  - EXACT match on the fully-normalized name only (no substring scans across 12k titles);
  - a first-3-tokens prefix key exists ONLY where that prefix is unambiguous across the map;
  - the same normalized title under two different CIKs -> ambiguous -> dropped entirely;
  - single generic words ("AMERICAN", "GENERAL", ...) never match; queries <5 chars never match.
"""
from __future__ import annotations

import os
import re
import time
import requests

from ..persist import load_json, save_json     # atomic + .bak — house persistence

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_PATH = "state/name_ticker_map.json"
CACHE_TTL = 7 * 24 * 3600.0    # legal names / listings barely move week to week
RETRY_WAIT = 900.0             # fetch failed -> stay polite to SEC, retry in 15 min
TIMEOUT = 15
# Same env/default as main.py builds cfg["_user_agent"] from (SEC politeness: identify yourself).
UA = os.getenv("TENTACLE_USER_AGENT", "TENTACLE research bot (contact: set TENTACLE_USER_AGENT)")

# Corporate boilerplate stripped off the END of a normalized name (repeatedly: "ARM HOLDINGS PLC"
# -> "arm"). "and" is here because "& CO" normalizes to "and co" and leaves a dangling "and";
# "new" because 13F issuer names style reorganized companies "CHEVRON CORP NEW".
_SUFFIX = {"inc", "corp", "corporation", "incorporated", "co", "company", "companies", "ltd",
           "limited", "plc", "lp", "llc", "holdings", "hldgs", "group", "trust", "sa", "nv",
           "ag", "the", "and", "new"}
# A single one of these words is never a company match ("AMERICAN" must not hit American Axle).
_GENERIC = {"american", "national", "first", "united", "general", "international", "global",
            "standard", "federal", "us", "usa", "new", "north", "south", "east", "west",
            "northern", "southern", "eastern", "western", "central", "pacific", "atlantic",
            "alliance", "liberty", "security", "energy", "capital", "financial"}

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")
# 891 of the ~12k SEC titles end in a state/registration tag — "BANK OF AMERICA CORP /DE/",
# "COSTCO WHOLESALE CORP /NEW", "ARM HOLDINGS PLC /UK", "Kioxia Holdings Corporation/ADR",
# "TOYOTA MOTOR CORP/". Applied to queries too, so both sides stay symmetric.
_SLASH_TAG_RE = re.compile(r"\s*/\s*[a-z]{0,4}\s*/?\s*$")

_maps: dict | None = None      # {"exact": {name: ticker}, "prefix3": {first-3-tokens: ticker}}
_refresh_at = 0.0              # when to next consult disk/SEC


def _clean(name: str) -> str:
    """casefold, drop trailing /DE/-style tag, &->and, punctuation->space, collapse whitespace."""
    s = _SLASH_TAG_RE.sub("", (name or "").casefold()).replace("&", " and ")
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", s)).strip()


def _strip(toks: list[str]) -> list[str]:
    """Drop a leading 'the' and trailing corporate suffixes — but never strip down to a lone
    GENERIC word ('NORTHERN TRUST CORP' keeps 'northern trust', not 'northern')."""
    if len(toks) > 1 and toks[0] == "the":
        toks = toks[1:]
    while len(toks) > 1 and toks[-1] in _SUFFIX:
        if len(toks) == 2 and toks[0] in _GENERIC:
            break
        toks = toks[:-1]
    return toks


def _key(name: str) -> str:
    return " ".join(_strip(_clean(name).split()))


def _build(rows) -> dict:
    """Normalized-title -> ticker. Share classes of the SAME issuer (GOOGL/GOOG, one CIK) keep
    the first row (the file is liquidity-ordered = primary class); the same title under
    DIFFERENT CIKs is genuinely ambiguous and gets dropped. Prefix keys likewise."""
    exact: dict[str, str] = {}
    prefix: dict[str, str] = {}
    cik_of: dict[str, int] = {}
    pcik_of: dict[str, int] = {}
    dead: set[str] = set()
    pdead: set[str] = set()
    for row in rows:
        try:
            tk, cik, key = str(row["ticker"]).upper(), int(row["cik_str"]), _key(str(row["title"]))
        except Exception:
            continue
        toks = key.split()
        if not tk or not toks or (len(toks) == 1 and (key in _GENERIC or len(key) < 2)):
            continue                                   # lookup guards would refuse it anyway
        if key not in exact:
            exact[key], cik_of[key] = tk, cik
        elif cik_of[key] != cik:
            dead.add(key)
        if len(toks) >= 3:
            p = " ".join(toks[:3])
            if p not in prefix:
                prefix[p], pcik_of[p] = tk, cik
            elif pcik_of[p] != cik:
                pdead.add(p)                           # ambiguous prefix -> never match
    for k in dead:
        exact.pop(k, None)
    for p in pdead:
        prefix.pop(p, None)
    return {"exact": exact, "prefix3": prefix}


def _get_maps() -> dict:
    """Load-once with a 7-day disk cache; on fetch failure degrade to the stale disk copy (or an
    empty in-memory map) and retry politely later — a resolver hiccup never sinks a tentacle."""
    global _maps, _refresh_at
    now = time.time()
    if _maps is not None and now < _refresh_at:
        return _maps
    disk = load_json(CACHE_PATH, {}) or {}
    if disk.get("exact") and now - float(disk.get("ts", 0)) < CACHE_TTL:
        _maps = {"exact": disk["exact"], "prefix3": disk.get("prefix3", {})}
        _refresh_at = float(disk["ts"]) + CACHE_TTL
        return _maps
    try:
        r = requests.get(SEC_URL, headers={"User-Agent": UA}, timeout=TIMEOUT)
        built = _build(r.json().values())
    except Exception as e:
        print(f"  [resolver] name map fetch failed (retry in {RETRY_WAIT/60:.0f}m): {e}")
        built = None
    if built and built["exact"]:
        _maps, _refresh_at = built, now + CACHE_TTL
        try:
            save_json(CACHE_PATH, {"ts": now, **built})
        except Exception:
            pass                                       # disk trouble -> in-memory only
    else:  # stale disk beats nothing
        if disk.get("exact"):
            _maps = {"exact": disk["exact"], "prefix3": disk.get("prefix3", {})}
        elif _maps is None:
            _maps = {"exact": {}, "prefix3": {}}
        _refresh_at = now + RETRY_WAIT
    return _maps


def resolve_name(name: str) -> str | None:
    """Company name -> ticker via the SEC issuer map, or None. Exact normalized match (plus the
    unambiguous 3-token prefix); anti-false-positive guards throughout — this feeds a trading
    signal, so None always beats a wrong ticker."""
    cleaned = _clean(name)
    if len(cleaned) < 5:                               # tiny fragments are never safe to match
        return None
    toks = _strip(cleaned.split())
    key = " ".join(toks)
    if not key or (len(toks) == 1 and (key in _GENERIC or len(key) < 2)):
        return None
    maps = _get_maps()
    tk = maps["exact"].get(key)
    if tk:
        return tk
    if len(toks) >= 3:                                 # e.g. "... CORPORATION GLOBAL SERVICES LLC"
        return maps["prefix3"].get(" ".join(toks[:3]))
    return None
