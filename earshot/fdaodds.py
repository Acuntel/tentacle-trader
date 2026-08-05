"""FDA prediction-market odds — the priced-in gauge for the approval catalysts.

Kalshi launched FDA / clinical-trial prediction markets 2026-07-16 (with AppliedXL); Polymarket
has run an FDA book since May. Both expose FREE public market data. For the fda_approval catalyst
(the ledger's one $-positive earner) the documented failure mode is "sell the news": when the
approval was already priced in, the pop fades. A prediction-market probability is a direct, live
measurement of HOW priced-in the event is:

  odds high (>= priced_in_prob)  -> the approval was expected -> drift is spent -> dampen/veto
  odds low  (<= surprise_prob)   -> genuine surprise          -> that's where the drift lives

The same sweep yields a free PDUFA-style CALENDAR: FDA event markets carry a close date, which
is the decision window — that feeds the pdufa_runup catalyst (sources/pdufa.py).

HONESTY RULES: markets with no real price (no last trade, no bid/ask) contribute NO odds — we
never invent a probability. Every fetch degrades gracefully to "no data" (odds_for -> None), in
which case the brain applies no conditioning at all. Kalshi outranks Polymarket on conflicts
(deeper post-launch book, cleaner ticker mapping).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_SEARCH = "https://gamma-api.polymarket.com/public-search"
# TWO-PART filter, both required (each alone leaked in testing): FDA context ("Will Trump approve
# X" matched a bare "approv"; "Who will Trump nominate as FDA commissioner?" matched a bare "fda")
# AND approval/trial context. A market must be about a REGULATORY DECISION on a product.
FDA_WORDS = ("fda", "pdufa")
APPROVAL_WORDS = ("approv", "authoriz", "clearance", "clears", "phase 3", "pdufa", "clinical trial")


def _is_fda_event(event_ticker: str, text_low: str) -> bool:
    fda_ctx = "FDA" in (event_ticker or "") or any(w in text_low for w in FDA_WORDS)
    return fda_ctx and any(w in text_low for w in APPROVAL_WORDS)
TICK_RE = re.compile(r"^[A-Z]{1,5}$")


def _parse_iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _kalshi_prob(m: dict) -> float | None:
    """A market's yes-probability from REAL prices only (cents -> 0..1). None = market has no price."""
    lp = m.get("last_price")
    if lp:
        return float(lp) / 100.0
    yb, ya = m.get("yes_bid"), m.get("yes_ask")
    if yb and ya:
        return (float(yb) + float(ya)) / 200.0
    return None


class FdaOdds:
    """TTL-cached sweep of both books. book = odds we can condition on; calendar = decision dates
    (known even when the market hasn't traded — the DATE is real, the price may not exist yet)."""
    REFRESH_SEC = 1800          # the odds move in hours/days, not seconds — stay polite
    MAX_PAGES = 15              # bounded Kalshi event pagination (200/page; sports flood the list)

    def __init__(self):
        self.book: dict[str, dict] = {}      # ticker -> {prob, close_ts, title, src}
        self.cal: dict[str, dict] = {}       # ticker -> {close_ts, title} (calendar, price optional)
        self.last = 0.0
        self.err = ""

    def _due(self) -> bool:
        return (time.time() - self.last) >= self.REFRESH_SEC

    def refresh(self):
        if not self._due():
            return
        self.last = time.time()
        book, cal = {}, {}
        try:
            self._sweep_polymarket(book, cal)    # first, so Kalshi overwrites on conflict
        except Exception as e:
            self.err = f"polymarket: {str(e)[:80]}"
        try:
            self._sweep_kalshi(book, cal)
        except Exception as e:
            self.err = f"kalshi: {str(e)[:80]}"
        # a failed sweep keeps the previous cache (stale-but-real beats empty); a clean sweep replaces it
        if book or cal:
            self.book, self.cal = book, cal

    def _sweep_kalshi(self, book: dict, cal: dict):
        cursor = None
        for _ in range(self.MAX_PAGES):
            params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(f"{KALSHI}/events", params=params, timeout=12)
            r.raise_for_status()
            d = r.json()
            for ev in d.get("events", []):
                et, title = ev.get("event_ticker", ""), ev.get("title", "")
                sub = ev.get("sub_title", "")
                low = (title + " " + sub).lower()
                if not _is_fda_event(et, low):
                    continue
                # VERIFIED-ONLY ticker mapping: the event-ticker suffix is only SOMETIMES the stock
                # (KXFDAVERVE102-VERV yes; KXFDAAPPROVE-MDMA is the DRUG). Trust the suffix only when
                # the SEC issuer map confirms the company's name appears in the market title; else
                # fall back to precise name extraction; else skip — no odds beats wrong odds.
                from .sources.resolver import ticker_matches_text, title_scan_ticker
                from .sources.base import extract_tickers
                tk = et.rsplit("-", 1)[-1] if "-" in et else ""
                if not (TICK_RE.match(tk) and ticker_matches_text(tk, title + " " + sub)):
                    tks = extract_tickers(title)
                    tk = tks[0] if len(tks) == 1 else (title_scan_ticker(title) or "")
                if not tk:
                    continue
                best_prob, best_close, near_close = None, None, None
                for m in ev.get("markets") or []:
                    ct = _parse_iso(m.get("close_time") or "")
                    if not ct or ct < time.time():
                        continue
                    if near_close is None or ct < near_close:
                        near_close = ct
                    p = _kalshi_prob(m)
                    if p is not None and (best_close is None or ct < best_close):
                        best_prob, best_close = p, ct   # nearest-dated PRICED market = the live odds
                if near_close:
                    cal[tk] = {"close_ts": near_close, "title": title[:120]}
                if best_prob is not None:
                    book[tk] = {"prob": round(best_prob, 3), "close_ts": best_close,
                                "title": title[:120], "src": "kalshi"}
            cursor = d.get("cursor")
            if not cursor:
                break

    def _sweep_polymarket(self, book: dict, cal: dict):
        from .sources.base import extract_tickers        # reuse the precise name->ticker machinery
        r = requests.get(POLY_SEARCH, params={"q": "FDA approve"}, timeout=12)
        r.raise_for_status()
        d = r.json()
        events = d.get("events", d if isinstance(d, list) else [])
        for ev in events if isinstance(events, list) else []:
            title = ev.get("title") or ev.get("question") or ""
            if not _is_fda_event("", title.lower()):
                continue
            end = _parse_iso(ev.get("endDate") or "")
            if not end or end < time.time():
                continue                                  # stale market — the search returns history too
            tks = extract_tickers(title)
            if len(tks) != 1:
                continue                                  # ambiguous mapping -> no odds, honestly
            prob = None
            for m in ev.get("markets") or []:
                try:
                    import json as _json
                    prices_ = m.get("outcomePrices")
                    vals = _json.loads(prices_) if isinstance(prices_, str) else prices_
                    if vals:
                        prob = float(vals[0])
                        break
                except Exception:
                    continue
            cal.setdefault(tks[0], {"close_ts": end, "title": title[:120]})
            if prob is not None:
                book.setdefault(tks[0], {"prob": round(prob, 3), "close_ts": end,
                                         "title": title[:120], "src": "polymarket"})

    def odds_for(self, ticker: str) -> dict | None:
        self.refresh()
        return self.book.get(ticker)

    def calendar(self) -> dict[str, dict]:
        self.refresh()
        return self.cal


_engine: FdaOdds | None = None


def engine() -> FdaOdds:
    global _engine
    if _engine is None:
        _engine = FdaOdds()
    return _engine
