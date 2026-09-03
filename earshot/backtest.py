"""THE BACKTEST GAUNTLET (ladder rung -1) — the gate that stands between shadow and real money.

⚠️ RAIL. selfcode/apply_patch may never edit this file. Rewritten 2026-09-02 by hand, with Brady's
approval, because the audit found it had become a dead artifact: the previous version ran ONCE (by
hand, 2026-06-18), tested naive earnings drift on 30 mega-caps — a strategy this bot does not run —
and nothing ever called it again. `state/gauntlet.json` has been a stale June file for ten weeks
while `executor._gauntlet_passed()` kept reading it as if it meant something.

What it measures NOW: the bot's OWN forward record, produced by the CURRENT configuration.

  1. MASTERY on the real book ledger (state/outcomes.jsonl) — the same price-based, cost-debited,
     market-adjusted math the live ladder uses. No re-labelling, no invented universe.
  2. POLICY EDGE from the replay harness — of the setups the bot actually saw, the ones the LIVE
     gate selects must beat the average setup it saw, with a Wilson lower bound above the field.
  3. SAMPLE SIZE — enough resolved trades that the number is not luck.
  4. FRESHNESS — the verdict is stamped with the ledger state it was computed from; a stale pass
     is not a pass (see executor._gauntlet_passed).

Every one of those must hold. The gate is strictly harder than the version it replaces: the old
one could pass on a historical backtest of a different strategy; this one can only pass on real
forward outcomes of the strategy actually running. It cannot manufacture an edge — it can only
refuse to certify one that is not there.

    python -m earshot.backtest            # print the verdict + write state/gauntlet.json
    python -m earshot.backtest --json
"""
from __future__ import annotations

import json
import os
import time

from .survival import compute_mastery

# --- the bars. Raising any of these makes the gate HARDER, which is always allowed. ---
MIN_BOOK_TRADES = 60        # resolved real-book outcomes before the number means anything
MIN_POLICY_N = 40           # labeled candidates the LIVE policy selected, in the replay window
REPLAY_DAYS = 45.0          # the replay window the policy edge is measured over
STALE_DAYS = 7.0            # a verdict older than this is not evidence about today's code
START_EQUITY = 500.0


def _book_rows(path: str = "state/outcomes.jsonl") -> list[dict]:
    """The immutable book ledger in the shape compute_mastery wants. Same alpha fallback the
    integrity audit uses, and the size fraction the drawdown term needs."""
    rows: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                a = o.get("alpha", o.get("pnl_pct"))
                if a is None:
                    continue
                rows.append({"conf": o.get("confidence", 50.0), "pnl_pct": o.get("pnl_pct", 0.0),
                             "alpha": a, "win": a > 0,
                             "size_frac": round(float(o.get("size_usd", 125.0)) / START_EQUITY, 4)})
    except FileNotFoundError:
        pass
    return rows


def evaluate(days: float = REPLAY_DAYS, now: float | None = None) -> dict:
    """Compute the verdict. Pure function of the ledgers on disk — no network, no randomness."""
    from . import replay
    from .patterns import field_rate
    from .persist import load_json

    rows = _book_rows()
    recent = rows[-100:]
    know = load_json("state/knowledge.json", {}) or {}
    try:
        field = field_rate(know)
    except Exception:
        field = 0.5
    m = compute_mastery(recent, START_EQUITY, field=field) if recent else {
        "mastery": 0.0, "n": 0, "win_rate": 0, "win_lb": 0, "profit_factor": 0,
        "max_dd": 0, "calibration": 0, "overconfidence": 0, "expectancy": 0}

    try:
        rp = replay.run(days, now=now)
    except Exception as e:                      # a replay failure must never fake a PASS
        rp = {"saw": {"n": 0}, "policy": {"n": 0, "alpha": None, "wilson_lb": None},
              "edge_vs_seen": None, "error": str(e)}

    pol, edge = rp.get("policy", {}), rp.get("edge_vs_seen")
    checks = {
        "book_samples": (len(rows) >= MIN_BOOK_TRADES,
                         f"{len(rows)}/{MIN_BOOK_TRADES} resolved book trades"),
        "policy_samples": (int(pol.get("n") or 0) >= MIN_POLICY_N,
                           f"{pol.get('n') or 0}/{MIN_POLICY_N} labeled policy picks in {days:.0f}d"),
        "policy_edge_positive": (edge is not None and edge > 0,
                                 f"policy edge vs everything seen = "
                                 f"{'n/a' if edge is None else format(edge, '+.3f')}%/trade"),
        "policy_beats_field": ((pol.get("wilson_lb") is not None and pol["wilson_lb"] > field),
                               f"policy Wilson-LB {pol.get('wilson_lb')} vs field {round(field, 3)}"),
        "book_profitable": (m.get("expectancy", 0) > 0,
                            f"book expectancy {m.get('expectancy')}%/trade (alpha)"),
    }
    failed = [k for k, (ok, _) in checks.items() if not ok]
    passed = not failed

    return {
        # `overall.mastery` is preserved for executor._gauntlet_passed's existing read. It is now
        # forced to 0 unless EVERY check holds, so the legacy numeric path can never pass alone.
        "overall": {**m, "mastery": (m["mastery"] if passed else 0.0)},
        "raw_mastery": m["mastery"],
        "passed": passed,
        "failed": failed,
        "checks": {k: {"ok": ok, "detail": d} for k, (ok, d) in checks.items()},
        "replay": {"days": days, "saw": rp.get("saw"), "policy": pol, "edge_vs_seen": edge},
        "field_rate": round(field, 4),
        "book_trades": len(rows),
        "ts": time.time(),
        "stale_after_days": STALE_DAYS,
        "version": "gauntlet-v3-forward-policy",
    }


def run(days: float = REPLAY_DAYS, write: bool = True) -> dict:
    d = evaluate(days)
    m = d["overall"]
    print("=" * 78)
    print("BACKTEST GAUNTLET v3 — the bot's OWN forward record (rung -1 -> real money)")
    print("=" * 78)
    print(f"  book: {d['book_trades']} resolved trades   mastery {d['raw_mastery']} "
          f"(win {m['win_rate']:.0%}, PF {m['profit_factor']}, maxDD {m['max_dd']:.0%}, "
          f"expectancy {m['expectancy']:+.2f}%)")
    rp = d["replay"]
    saw, pol = rp.get("saw") or {}, rp.get("policy") or {}
    print(f"  replay ({rp['days']:.0f}d): saw n={saw.get('n', 0)}  policy n={pol.get('n', 0)}  "
          f"edge {'n/a' if rp['edge_vs_seen'] is None else format(rp['edge_vs_seen'], '+.3f')}%/trade")
    print("-" * 78)
    for k, c in d["checks"].items():
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {k:<22} {c['detail']}")
    print("-" * 78)
    print(f"  VERDICT: {'PASS — eligible to begin the live ladder' if d['passed'] else 'FAIL — stays in shadow'}"
          + ("" if d["passed"] else f"  (blocked by: {', '.join(d['failed'])})"))
    print("  Real money additionally requires: identity.mode=live, a live rung, and a manual arm.")
    print("=" * 78)
    if write:
        os.makedirs("state", exist_ok=True)
        from .persist import save_json
        save_json("state/gauntlet.json", d)
        print("  saved -> state/gauntlet.json")
    return d


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="backtest", description="Run the Backtest Gauntlet")
    ap.add_argument("--days", type=float, default=REPLAY_DAYS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    d = run(a.days, write=not a.no_write) if not a.json else evaluate(a.days)
    if a.json:
        print(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
