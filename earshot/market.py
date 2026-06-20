"""Market clock — the bot must KNOW when the exchange is open. A closed market can't be traded.

Pure-stdlib NYSE calendar: regular hours 09:30-16:00 ET, weekends, US market holidays
(incl. Juneteenth), and 13:00 ET half-days. Eastern time is derived from the US DST rule directly,
so this needs no `tzdata` package on a slim container. `session()` is the single source of truth;
`can_open()` is what risk/main call to refuse entries when the tape is shut.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date

REG_OPEN = (9, 30)
REG_CLOSE = (16, 0)
EARLY_CLOSE = (13, 0)
PRE_OPEN = (4, 0)
AFTER_CLOSE = (20, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (0=Mon) of a month; n<0 means counting from the end (use -1 for 'last')."""
    if n > 0:
        d = date(year, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    d = (date(year, 12, 31) if month == 12
         else date(year, month + 1, 1) - timedelta(days=1))
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e, f = b // 4, b % 4, (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """NYSE shifts a holiday off the weekend: Sat->Fri, Sun->Mon."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holiday_map(year: int) -> dict[date, str]:
    h: dict[date, str] = {}
    ny = date(year, 1, 1)                       # New Year's: no Friday makeup when it lands on Sat
    if ny.weekday() == 6:
        h[date(year, 1, 2)] = "New Year's Day"
    elif ny.weekday() != 5:
        h[ny] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "MLK Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    h[_easter(year) - timedelta(days=2)] = "Good Friday"
    h[_nth_weekday(year, 5, 0, -1)] = "Memorial Day"
    h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    h[_observed(date(year, 12, 25))] = "Christmas"
    return h


def early_closes(year: int) -> set[date]:
    e: set[date] = set()
    j3 = date(year, 7, 3)
    if j3.weekday() < 5 and date(year, 7, 4).weekday() < 5:
        e.add(j3)                                            # July 3 half-day
    e.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # day after Thanksgiving
    xe = date(year, 12, 24)
    if xe.weekday() < 5:
        e.add(xe)                                            # Christmas Eve half-day
    return e


def _in_dst(dt_utc: datetime) -> bool:
    """US DST: 2nd Sun of March 07:00 UTC -> 1st Sun of Nov 06:00 UTC (the 2am-local switch)."""
    y = dt_utc.year
    s = _nth_weekday(y, 3, 6, 2)
    start = datetime(s.year, s.month, s.day, 7, tzinfo=timezone.utc)
    n = _nth_weekday(y, 11, 6, 1)
    end = datetime(n.year, n.month, n.day, 6, tzinfo=timezone.utc)
    return start <= dt_utc < end


def now_et(dt_utc: datetime | None = None) -> datetime:
    dt_utc = dt_utc or datetime.now(timezone.utc)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc + timedelta(hours=-4 if _in_dst(dt_utc) else -5)


def _next_open(dt_utc: datetime) -> str:
    """The next regular open as a friendly ET string (scans up to ~10 days ahead)."""
    et = now_et(dt_utc)
    for _ in range(10):
        d = et.date()
        is_trading = d.weekday() < 5 and d not in holiday_map(d.year)
        before_open = et.hour * 60 + et.minute < REG_OPEN[0] * 60 + REG_OPEN[1]
        if is_trading and (before_open or et.date() != now_et(dt_utc).date()):
            return f"{d:%a %b %d} 09:30 ET"
        et = datetime(d.year, d.month, d.day) + timedelta(days=1)
    return "?"


def session(dt_utc: datetime | None = None) -> dict:
    """Single source of truth: {open, session, reason, et, next_open}."""
    et = now_et(dt_utc)
    d = et.date()
    minutes = et.hour * 60 + et.minute
    half = d in early_closes(d.year)
    close = (EARLY_CLOSE if half else REG_CLOSE)
    reg_open = REG_OPEN[0] * 60 + REG_OPEN[1]
    reg_close = close[0] * 60 + close[1]
    info = {"open": False, "session": "closed", "reason": "overnight",
            "et": et.strftime("%a %Y-%m-%d %H:%M ET"), "next_open": _next_open(dt_utc)}
    if d.weekday() >= 5:
        info.update(session="weekend", reason="weekend")
    elif d in holiday_map(d.year):
        info.update(session="holiday", reason=f"{holiday_map(d.year)[d]} (market holiday)")
    elif reg_open <= minutes < reg_close:
        info.update(open=True, session="regular",
                    reason="regular session" + (" — half-day, closes 13:00 ET" if half else ""))
    elif PRE_OPEN[0] * 60 <= minutes < reg_open:
        info.update(session="premarket", reason="pre-market")
    elif reg_close <= minutes < AFTER_CLOSE[0] * 60:
        info.update(session="afterhours", reason="after-hours")
    return info


def can_open(market_cfg: dict, dt_utc: datetime | None = None) -> tuple[bool, str, dict]:
    """Should the bot be allowed to OPEN a position right now?"""
    s = session(dt_utc)
    if not market_cfg.get("require_open", True):
        return True, "market gate disabled", s
    if s["session"] == "regular":
        return True, "market open (regular session)", s
    if market_cfg.get("allow_extended_hours") and s["session"] in ("premarket", "afterhours"):
        return True, f"market open ({s['session']})", s
    return False, f"market CLOSED — {s['reason']}; next open {s['next_open']}", s


def was_closed(ts: float) -> bool:
    """Was the regular market shut at this epoch time? (used to audit past entries)."""
    return session(datetime.fromtimestamp(ts, timezone.utc))["session"] != "regular"
