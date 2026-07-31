"""The daily review — ONE Telegram message, written like a firm reporting to its client.

Brady's spec (2026-07-29, supersedes 07-25): lead with exactly four data points, one metric
per line — account balance, amount invested, return on those investments, and what's held
(with allocations). Then the decision rule's RESULTS: which approaches were kept because
they're profitable and which were stopped because they aren't. Plain language, zero jargon.

Composes exclusively from the real ledgers/state (never invents numbers); HTML-ready for
notify.send. The old digest (Brier/P(win)/dir-hit language) lives on in EVOLUTION_LOG.md
for the deep read — this is the phone-screen view.
"""
from __future__ import annotations

import json
import os
import time

from . import market, notify, prices

# catalyst -> how a human would say it
PLAIN_CAT = {
    "fda_approval": "FDA approval", "earnings_beat": "earnings beat",
    "gov_contract": "government contract", "insider_cluster": "insider buying",
    "index_inclusion": "joining an index", "mna_target": "buyout news",
    "activist_stake": "activist investor", "demand_surge": "product demand jump",
    "ai_quantum_breakout": "AI/quantum theme", "unknown": "unclassified news",
}

PLAIN_EXIT = {
    "stop": "stopped out", "target": "hit the profit target",
    "trailing": "locked in the gain as it slipped", "time": "time was up",
    "decay": "the story faded",
}


def _plain_cat(cat: str) -> str:
    return PLAIN_CAT.get(cat, (cat or "unknown").replace("_", " "))


def _plain_exit(reason: str) -> str:
    r = (reason or "").split(":")[0]
    return PLAIN_EXIT.get(r, "sold on a warning sign")


def _et_day(ts: float) -> str:
    from datetime import datetime, timezone
    return market.now_et(datetime.fromtimestamp(ts, timezone.utc)).strftime("%Y-%m-%d")


def _today_et() -> str:
    return market.now_et().strftime("%Y-%m-%d")


def _read_jsonl(path: str, tail_bytes: int = 300_000) -> list[dict]:
    """Cheap tail-read of an append-only ledger (today's rows live at the end)."""
    try:
        size = os.path.getsize(path)
        with open(path) as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()                       # drop the partial line
            return [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []


def _sign_usd(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


def compose(learner, instinct, core, cfg: dict) -> str:
    """core is unused since the 07-29 format (kept for call-site compatibility)."""
    e = notify.esc
    today = _today_et()
    nice_day = market.now_et().strftime("%a %b %-d")

    ledger = _read_jsonl("state/outcomes.jsonl", 2_000_000)   # the whole audited book (small file)
    sold = [o for o in ledger if _et_day(o.get("exit_ts", 0)) == today]
    bought = [p for p in learner.open if _et_day(p.get("entry_ts", 0)) == today]
    bought += [o for o in sold if _et_day(o.get("entry_ts", 0)) == today]  # same-day round trips
    day_pnl = round(sum(o.get("pnl_usd", 0.0) for o in sold), 2)

    # ---- mark the book to market: every number below comes from ledgers + live prices ----
    stt = instinct.status()
    realized_eq = stt.get("paper_equity", 0.0)             # start + all realized P&L
    rows = []                                              # (ticker, cost, mkt_value, ret_pct|None, catalyst)
    for p in learner.open:
        cost = float(p.get("size_usd", 0.0))
        px, entry = prices.last_price(p["ticker"]), p.get("entry_price")
        if px and entry:
            rows.append((p["ticker"], cost, cost * px / entry, (px / entry - 1) * 100, p.get("catalyst")))
        else:
            rows.append((p["ticker"], cost, cost, None, p.get("catalyst")))
    invested_cost = sum(r[1] for r in rows)
    invested_mkt = sum(r[2] for r in rows)
    total = realized_eq - invested_cost + invested_mkt     # account marked to market
    cash = max(0.0, realized_eq - invested_cost)
    inv_ret = (invested_mkt / invested_cost - 1) * 100 if invested_cost else 0.0

    L: list[str] = [f"🐙 <b>TENTACLE — daily report · {e(nice_day)}</b>", ""]

    # ---- THE FOUR NUMBERS (Brady's spec: one metric per line, nothing that needs a glossary) ----
    L.append(f"💰 Account total: <b>${total:,.2f}</b> (practice money — started with $500).")
    L.append(f"Invested: ${invested_mkt:,.2f} in {len(rows)} stock{'s' if len(rows) != 1 else ''} · "
             f"Cash: ${cash:,.2f}.")
    if rows:
        L.append(f"Return on current investments: <b>{inv_ret:+.1f}%</b> "
                 f"({_sign_usd(invested_mkt - invested_cost)}).")
    else:
        L.append("Return on current investments: nothing invested right now.")
    # Day-over-day truth: compare against the last report from a PREVIOUS day (snapshot survives
    # multiple same-day sends), and say how much of the move was actually locked in by sales.
    snap_path = "state/report_snapshot.json"
    try:
        snap = json.load(open(snap_path))
    except Exception:
        snap = {}
    baseline = snap.get("prev_total") if snap.get("date") == today else snap.get("total")
    if baseline is not None:
        L.append(f"Change since the last report: {_sign_usd(total - float(baseline))} "
                 f"(locked in by sales today: {_sign_usd(day_pnl)}).")
    else:
        L.append(f"Locked in by sales today: {_sign_usd(day_pnl)}.")
    try:
        json.dump({"date": today, "total": round(total, 2),
                   "prev_date": snap.get("prev_date") if snap.get("date") == today else snap.get("date"),
                   "prev_total": baseline}, open(snap_path, "w"))
    except Exception:
        pass
    L.append(f"Real dollars at risk: $0 — locked until the practice score reaches "
             f"{stt.get('needs_mastery', '?')} (now {stt.get('mastery', 0):.0f}).")
    L.append("")

    # ---- WHAT YOU OWN (allocation per holding) ----
    L.append(f"📦 <b>WHAT IT OWNS ({len(rows)})</b>")
    for tkr, cost, mkt, ret, cat in rows[:8]:
        pct_of = f"{mkt / total * 100:.0f}% of the account" if total > 0 else "—"
        ret_s = f"{ret:+.1f}%" if ret is not None else "price pending"
        L.append(f"• {e(tkr)} — ${mkt:,.0f} ({pct_of}) · {ret_s} · {e(_plain_cat(cat))}")
    if len(rows) > 8:
        L.append(f"• …and {len(rows) - 8} more")
    if not rows:
        L.append("• nothing — all cash, waiting for a setup worth buying")
    L.append("")

    # ---- TODAY'S ACTIVITY (one line each; details live in the ledger) ----
    L.append("🛒 <b>TODAY</b>")
    if bought:
        L.append("Bought: " + ", ".join(f"{e(p['ticker'])} (${p.get('size_usd', 0):,.0f}, "
                                        f"{e(_plain_cat(p.get('catalyst')))})" for p in bought[:5])
                 + (f" +{len(bought) - 5} more" if len(bought) > 5 else "") + ".")
    else:
        L.append("Bought: nothing — no setup was good enough today.")
    if sold:
        L.append("Sold: " + ", ".join(f"{e(o['ticker'])} {o.get('pnl_pct', 0):+.1f}% "
                                      f"({e(_plain_exit(o.get('exit', '')))})" for o in sold[:5])
                 + (f" +{len(sold) - 5} more" if len(sold) > 5 else "") + ".")
    else:
        L.append("Sold: nothing.")
    L.append("")

    # ---- THE DECISION RULE'S RESULTS, in DOLLARS (implemented, not proposed). Evidence = the
    # book's own audited ledger — the same file equity is recomputed from. Observation labels no
    # longer vote here (07-29 fix: they were 98.5% of the old ledger and buried the real record). ----
    pm = learner.patterns
    base = int(cfg.get("exploration", {}).get("max_per_catalyst", 4))
    book: dict = {}
    for o in ledger:
        d = book.setdefault(o.get("catalyst") or "unknown", {"n": 0, "usd": 0.0})
        d["n"] += 1
        d["usd"] += o.get("pnl_usd", 0.0)
    working, stopped, watching = [], [], []
    for cat, d in sorted(book.items(), key=lambda kv: -kv[1]["usd"]):
        row = f"{e(_plain_cat(cat))}: {_sign_usd(d['usd'])} over {d['n']} trade{'s' if d['n'] != 1 else ''}"
        slots = pm.catalyst_explore_slots(cat, base)
        if d["usd"] > 0:
            working.append(row + (" — kept, proven, extra budget" if pm.catalyst_graduated(cat)
                                  else " — kept, extra tryout to finish proving it" if slots > base
                                  else " — kept"))
        elif cat == "unknown":
            stopped.append(row + " — capped at one tryout at a time (discovery pool)")
        elif pm.catalyst_dead(cat) or slots == 0:
            stopped.append(row + " — stopped entirely")
        elif slots < base:
            stopped.append(row + " — budget cut to a minimum until it earns it back")
        else:
            watching.append(f"{e(_plain_cat(cat))} ({_sign_usd(d['usd'])}, {d['n']} trades)")
    L.append(f"✅ <b>MAKING MONEY — KEPT ({len(working)})</b>")
    L += [f"• {r}" for r in working[:5]] or ["• none right now"]
    L.append("")
    L.append(f"❌ <b>LOSING MONEY — CUT ({len(stopped)})</b>")
    L += [f"• {r}" for r in stopped[:5]] or ["• nothing needed cutting"]
    if watching:
        L.append(f"👀 Too few trades to judge yet: {', '.join(watching[:4])}.")
    n_all = sum(d["n"] for d in book.values())
    proven = [c for c in book if pm.catalyst_graduated(c)]
    L.append(f"Proven long-term (30+ of its own trades, beats the account's average): "
             + (", ".join(e(_plain_cat(c)) for c in proven) + "."
                if proven else f"none yet — the account has {n_all} finished trades total, "
                               f"so a green day is a data point, not proof."))
    return "\n".join(L)[:3900]


if __name__ == "__main__":
    # On-demand review: `python -m earshot.report` prints it; `--send` also texts it via Telegram.
    # Run inside the deployed container (railway ssh) to see exactly what Brady's phone gets.
    # Bypasses the once-a-day gate on purpose — the scheduled after-close send is untouched.
    import sys
    import yaml
    cfg = yaml.safe_load(open("config.yaml"))
    from .learning import Learner
    from .survival import MotherInstinct
    from .core import BrainCore
    from .learnedgate import LearnedGate
    weights = dict(cfg["source_weights"])
    ln = Learner(weights, priors=cfg["source_weights"])
    ln.lgate = LearnedGate(ln.know)
    msg = compose(ln, MotherInstinct(cfg), BrainCore(ln.know, weights, cfg), cfg)
    print(msg)
    if "--send" in sys.argv:
        print("sent" if notify.send(msg) else "NOT sent (Telegram creds missing?)")
