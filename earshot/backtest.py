"""The Backtest Gauntlet (ladder rung -1) — smart version.

Tests the bot's #1 drift catalyst honestly: EARNINGS DRIFT (PEAD), classified by the REAL
earnings surprise (beat vs miss) from free yfinance data. Crucially it enters on the DRIFT —
the trading day AFTER the report — because we can't win the millisecond gap on Robinhood; we
ride the documented post-earnings continuation. Aggregates into the same Mastery score the
live bot uses.

Run:  python -m earshot.backtest
"""
from __future__ import annotations

import json
import math
import os
import time

from .survival import compute_mastery

UNIVERSE = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD", "AVGO", "PLTR",
            "LMT", "RTX", "NOC", "OXY", "PFE", "MRNA", "LLY", "GILD", "INTC", "MU",
            "CRM", "ORCL", "QCOM", "NFLX", "COST", "WMT", "JPM", "BAC", "DIS", "UBER"]
HOLD_DAYS = 5
STOP, TARGET = 8.0, 12.0
BEAT_THRESH = 0.02      # >2% surprise = a beat we'd trade
SCALE = 200             # surprise 0.10 -> +20 conf


def _num(x):
    try:
        f = float(x)
        return None if math.isnan(f) else f
    except Exception:
        return None


def run():
    try:
        import yfinance as yf
    except Exception:
        print("yfinance required (pip install yfinance in the py3.11 venv)."); return

    print("=" * 76)
    print(f"BACKTEST GAUNTLET v2 — EARNINGS DRIFT (PEAD), {len(UNIVERSE)} tickers, {HOLD_DAYS}d hold")
    print("  strategy: buy the DRIFT (day after a positive surprise), hold 5d, stop/target capped")
    print("=" * 76)

    beats, misses, allev = [], [], []
    for tk in UNIVERSE:
        t = yf.Ticker(tk)
        try:
            ed = t.get_earnings_dates(limit=20)
            hist = t.history(period="3y")
        except Exception as e:
            print(f"  {tk:<6} skip ({e})"); continue
        if ed is None or len(ed) == 0 or len(hist) < HOLD_DAYS + 5:
            print(f"  {tk:<6} no data"); continue
        closes = list(hist["Close"])
        dates = [d.strftime("%Y-%m-%d") for d in hist.index]
        n = 0
        for ts, row in ed.iterrows():
            rep, est = _num(row.get("Reported EPS")), _num(row.get("EPS Estimate"))
            if rep is None or est is None or est == 0:
                continue  # future / missing
            surprise = (rep - est) / abs(est)
            d = ts.strftime("%Y-%m-%d")
            i = next((k for k, dd in enumerate(dates) if dd > d), None)  # first day AFTER report
            if i is None or i + HOLD_DAYS >= len(closes):
                continue
            ret = max(-STOP, min(TARGET, (closes[i + HOLD_DAYS] / closes[i] - 1) * 100))
            conf = min(95, 55 + min(40, abs(surprise) * SCALE))
            rec = {"conf": conf, "pnl_pct": ret, "win": ret > 0, "surprise": surprise}
            allev.append(rec)
            if surprise > BEAT_THRESH:
                beats.append(rec); n += 1
            elif surprise < -BEAT_THRESH:
                misses.append(rec)
        print(f"  {tk:<6} {n} tradeable beats")
        time.sleep(0.1)

    print("\n" + "-" * 76)
    for label, recs in (("BEATS (traded)", beats), ("MISSES (control)", misses), ("ALL", allev)):
        if recs:
            m = compute_mastery(recs, 500.0)
            print(f"  {label:<16} n={m['n']:<4} win={m['win_rate']:.0%}  "
                  f"avg={m['expectancy']:+.2f}%  PF={m['profit_factor']}  maxDD={m['max_dd']:.0%}  "
                  f"mastery={m['mastery']}")
    print("-" * 76)
    g = compute_mastery(beats, 500.0)
    print(f"  GAUNTLET MASTERY (beats) = {g['mastery']}   bars: 80 -> Live Shadow, 92 -> Full live")
    verdict = ("PASS (>=80) -> eligible to start the live-shadow ladder" if g["mastery"] >= 80
               else "FAIL (<80) -> keep improving in shadow before any live money")
    print(f"  VERDICT: {verdict}")
    print("=" * 76)

    os.makedirs("state", exist_ok=True)
    json.dump({"overall": g, "beats": g,
               "misses": compute_mastery(misses, 500.0) if misses else None,
               "ts": time.time()}, open("state/gauntlet.json", "w"), indent=2)
    print("  saved -> state/gauntlet.json")


if __name__ == "__main__":
    run()
