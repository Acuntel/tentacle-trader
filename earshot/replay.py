"""THE REPLAY HARNESS — the one deterministic answer to "did that change help?".

The autopsy of 2026-09-01 found the self-improvement loop could not tell what helped: 39 of 60
verdicts were "unattributable", the CI fitness gate measured one model's Brier over 400 rows (so
any patch outside that model shipped as "no worse by construction"), and the Gauntlet had been a
stale June artifact for ten weeks. None of them measured THE POLICY — which names the bot would
actually buy.

This module does. Every cycle the brain records the candidates it considered, with the verdict of
every gate, to `state/candidates.jsonl`. Those rows are joined to the realized market outcome for
the same ticker on the same trading day (from the practice ledger the explore lane already writes,
and from the real book). Replaying them answers, deterministically and offline:

    of the setups the bot SAW, which ones does today's policy select, and what did they do?

That makes a code change measurable in the only currency that matters: the expectancy of the set
the policy picks, against the accept-all baseline of everything it saw. No LLM, no network, no
randomness — the same rows in give the same number out, so two builds are comparable.

    python -m earshot.replay              # current policy vs accept-all, last 30 days
    python -m earshot.replay --days 14    # a shorter window
    python -m earshot.replay --json       # machine-readable (the CI fitness gate reads this)
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

CAND_PATH = "state/candidates.jsonl"          # what the brain saw + every gate's verdict
LEARN_OUT = "state/learning_outcomes.jsonl"   # realized practice outcomes (the dense label source)
BOOK_OUT = "state/outcomes.jsonl"             # realized book outcomes (sparse, but real money-shaped)

# A candidate is joined to an outcome for the SAME ticker on the SAME ET trading day. The practice
# lane opens at most one trade per ticker per day (learn_seen), so the join is 1:1 by construction.
MAX_JOIN_DAYS = 3.0        # a label this many days after the sighting still describes that setup


def _iso_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


def _parse_ts(v) -> float | None:
    """Rows carry either an epoch float or an ISO string, depending on the writer."""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _read(path: str, limit: int = 200_000) -> list[dict]:
    """Read a jsonl ledger plus any rotated siblings, newest rows last. Bad lines are skipped, not
    fatal: a half-written final line must never take down a measurement."""
    rows: list[dict] = []
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    try:
        names = sorted(f for f in os.listdir(d) if f == base or f.startswith(base + "."))
    except OSError:
        return rows
    for name in names:
        try:
            with open(os.path.join(d, name)) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except OSError:
            continue
    return rows[-limit:]


def _wilson_lower(wins: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (c - m) / d)


def load_labeled(days: float = 30.0, now: float | None = None) -> list[dict]:
    """Recorded candidates joined to what the market actually did. One row per (ticker, day).

    The label is `alpha` — return minus SPY over the same window, net of the liquidity-tiered
    round trip — exactly what every other learner in the system is trained on, so a replay number
    is comparable to a live gauge. Practice outcomes are preferred (dense, fixed discipline); a
    book outcome is used when that is all there is.
    """
    now = float(now if now is not None else time.time())
    cutoff = now - days * 86400

    labels: dict[tuple, dict] = {}
    for r in _read(LEARN_OUT):
        ts = _parse_ts(r.get("ts"))
        # long-side labels only: the account is long-only, so a short's label describes a trade
        # that could never be taken (and the 5 corrupt reverse-split shorts lived here).
        if ts is None or int(r.get("dir", 1)) <= 0 or (r.get("exit") == "corrupt"):
            continue
        a = r.get("alpha")
        if a is None or abs(float(a)) > 60.0:
            continue
        labels[(r.get("ticker"), _iso_day(ts))] = {"alpha": float(a), "ts": ts, "src": "practice"}
    for r in _read(BOOK_OUT):
        ts = _parse_ts(r.get("entry_ts"))
        if ts is None:
            continue
        a = r.get("alpha", r.get("pnl_pct"))
        if a is None:
            continue
        labels.setdefault((r.get("ticker"), _iso_day(ts)), {"alpha": float(a), "ts": ts, "src": "book"})

    out: list[dict] = []
    seen: set[tuple] = set()
    for c in _read(CAND_PATH):
        ts = _parse_ts(c.get("ts"))
        if ts is None or ts < cutoff:
            continue
        key = (c.get("ticker"), _iso_day(ts))
        if key in seen:                       # one sighting per ticker-day: the first one
            continue
        lab = labels.get(key)
        if not lab or abs(lab["ts"] - ts) > MAX_JOIN_DAYS * 86400:
            continue
        seen.add(key)
        out.append({**c, "alpha": lab["alpha"], "label_src": lab["src"]})
    return out


def expectancy(rows: list[dict]) -> dict:
    """The scorecard for a selected set: mean alpha per trade, hit rate, and a Wilson lower bound
    so a lucky handful cannot look like an edge."""
    n = len(rows)
    if not n:
        return {"n": 0, "alpha": None, "hit": None, "wilson_lb": None, "total_alpha": 0.0}
    a = [float(r["alpha"]) for r in rows]
    wins = sum(1 for x in a if x > 0)
    return {"n": n, "alpha": round(sum(a) / n, 3), "hit": round(wins / n, 3),
            "wilson_lb": round(_wilson_lower(wins, n), 3), "total_alpha": round(sum(a), 2)}


def selected(rows: list[dict], policy=None) -> list[dict]:
    """The subset today's policy would actually BUY.

    Default policy = the recorded live verdict: `would_pass_live`, which main stamps from the same
    gate the live book uses. Pass a callable to ask a counterfactual ("what if we required a
    trigger family?") over the same rows.
    """
    if policy is None:
        return [r for r in rows if r.get("would_pass_live")]
    return [r for r in rows if policy(r)]


def run(days: float = 30.0, now: float | None = None) -> dict:
    """Replay the window: what the policy picked vs everything it saw, plus the per-catalyst cut.

    `edge` is the number that matters — how much better (in alpha per trade) the policy's picks did
    than the average setup it looked at. A policy with no edge is a filter that is not filtering.
    """
    rows = load_labeled(days, now=now)
    sel = selected(rows)
    base, pick = expectancy(rows), expectancy(sel)
    by_cat: dict[str, dict] = {}
    for cat in sorted({str(r.get("catalyst", "unknown")) for r in sel}):
        by_cat[cat] = expectancy([r for r in sel if str(r.get("catalyst", "unknown")) == cat])
    blocked: dict[str, int] = {}
    for r in rows:
        if not r.get("would_pass_live"):
            blocked[str(r.get("blocked_by") or "unknown")] = blocked.get(str(r.get("blocked_by") or "unknown"), 0) + 1
    edge = (round(pick["alpha"] - base["alpha"], 3)
            if (pick["alpha"] is not None and base["alpha"] is not None) else None)
    return {"days": days, "saw": base, "policy": pick, "edge_vs_seen": edge,
            "by_catalyst": by_cat, "blocked_by": dict(sorted(blocked.items(), key=lambda kv: -kv[1])[:8]),
            "labels": {"practice": sum(1 for r in rows if r.get("label_src") == "practice"),
                       "book": sum(1 for r in rows if r.get("label_src") == "book")}}


def _fmt(d: dict) -> str:
    s, p = d["saw"], d["policy"]
    L = ["=" * 78, f"REPLAY — the last {d['days']:.0f} days of recorded candidates", "=" * 78]
    if not s["n"]:
        L += ["  no labeled candidates in this window yet.",
              "  (the recorder ships with this build — give it a few sessions of market hours)", "=" * 78]
        return "\n".join(L)
    L += [f"  saw     n={s['n']:<5} alpha {s['alpha']:+.3f}%/trade  hit {s['hit']:.0%}",
          f"  policy  n={p['n']:<5} " + (f"alpha {p['alpha']:+.3f}%/trade  hit {p['hit']:.0%}  "
                                         f"wilson-LB {p['wilson_lb']:.3f}" if p["n"] else "(selected nothing)"),
          f"  EDGE vs everything it saw: "
          + (f"{d['edge_vs_seen']:+.3f}%/trade" if d["edge_vs_seen"] is not None else "n/a"),
          "-" * 78]
    if d["by_catalyst"]:
        L.append("  per catalyst (policy picks only):")
        for cat, e in sorted(d["by_catalyst"].items(), key=lambda kv: -(kv[1]["alpha"] or 0)):
            L.append(f"    {cat:<22} n={e['n']:<4} alpha {e['alpha']:+.3f}%  hit {e['hit']:.0%}  "
                     f"wilson-LB {e['wilson_lb']:.3f}")
    if d["blocked_by"]:
        L += ["-" * 78, "  what the gate rejected:"]
        for why, n in d["blocked_by"].items():
            L.append(f"    {n:>5}  {why[:64]}")
    L += ["-" * 78,
          f"  labels: {d['labels']['practice']} practice / {d['labels']['book']} book",
          "  NOTE: this is a REPLAY of setups the bot really saw, not a backtest of an invented",
          "  strategy. It cannot prove an edge that is not in the rows — it can only measure one.",
          "=" * 78]
    return "\n".join(L)


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="replay", description="Replay recorded candidates through the current policy")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()
    d = run(a.days)
    print(json.dumps(d, indent=2) if a.json else _fmt(d))


if __name__ == "__main__":
    main()
