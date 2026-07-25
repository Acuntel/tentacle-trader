"""The daily review — ONE Telegram message, written like a morning stock note.

Brady's spec (2026-07-25): what was bought and sold today, a count and a plain outcome for
every section, what's working and what isn't (and that the losers were CUT), zero jargon.
One scan = the full picture. Anything that needs a glossary does not belong in this message.

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


def compose(learner, instinct, core, cfg: dict) -> str:
    e = notify.esc
    today = _today_et()
    nice_day = market.now_et().strftime("%a %b %-d")

    # ---- the book: sold today (realized), bought today, still holding ----
    sold = [o for o in _read_jsonl("state/outcomes.jsonl")
            if _et_day(o.get("exit_ts", 0)) == today]
    bought = [p for p in learner.open if _et_day(p.get("entry_ts", 0)) == today]
    bought += [o for o in sold if _et_day(o.get("entry_ts", 0)) == today]  # same-day round trips
    holding = list(learner.open)

    stt = instinct.status()
    eq = stt.get("paper_equity", 0.0)
    day_pnl = round(sum(o.get("pnl_usd", 0.0) for o in sold), 2)

    L: list[str] = [f"🐙 <b>TENTACLE — daily review · {e(nice_day)}</b>", ""]

    # ---- MONEY ----
    L.append(f"💰 <b>MONEY</b>  practice account ${eq:.0f} (started $500) · today "
             f"{'+' if day_pnl >= 0 else '−'}${abs(day_pnl):.2f}")
    L.append(f"Real money: $0 at risk — stays locked until it proves itself "
             f"({stt.get('mastery', 0):.0f}/{stt.get('needs_mastery', '?')} toward unlocking the first $25).")
    L.append("")

    # ---- BOUGHT ----
    L.append(f"🛒 <b>BOUGHT ({len(bought)})</b>")
    if bought:
        for p in bought[:8]:
            L.append(f"• {e(p['ticker'])} ${p.get('size_usd', 0):.0f} — {e(_plain_cat(p.get('catalyst')))}")
        if len(bought) > 8:
            L.append(f"• …and {len(bought) - 8} more")
    else:
        L.append("• nothing — no setup was good enough today (that's discipline, not laziness)")
    L.append("")

    # ---- SOLD ----
    L.append(f"💸 <b>SOLD ({len(sold)})</b>")
    if sold:
        for o in sold[:8]:
            sign = "+" if o.get("pnl_usd", 0) >= 0 else "−"
            L.append(f"• {e(o['ticker'])} {o.get('pnl_pct', 0):+.1f}% ({sign}${abs(o.get('pnl_usd', 0)):.2f}) "
                     f"— {e(_plain_exit(o.get('exit', '')))}")
        if len(sold) > 8:
            L.append(f"• …and {len(sold) - 8} more")
    else:
        L.append("• nothing sold today")
    L.append("")

    # ---- HOLDING ----
    L.append(f"📦 <b>HOLDING ({len(holding)})</b>")
    for p in holding[:8]:
        px = prices.last_price(p["ticker"])
        if px and p.get("entry_price"):
            ur = (px / p["entry_price"] - 1) * 100
            L.append(f"• {e(p['ticker'])} {ur:+.1f}% — {e(_plain_cat(p.get('catalyst')))}")
        else:
            L.append(f"• {e(p['ticker'])} — {e(_plain_cat(p.get('catalyst')))}")
    if not holding:
        L.append("• all cash")
    L.append("")

    # ---- WHAT'S WORKING / NOT WORKING (forward record only; losers get their budget CUT) ----
    pm = learner.patterns
    fld = pm.field()
    working, cut, testing = [], [], []
    base = int(cfg.get("exploration", {}).get("max_per_catalyst", 4))
    for cat, st in sorted(pm.grad.items(), key=lambda kv: -kv[1].get("trades", 0)):
        n = st.get("trades", 0)
        if not n:
            continue
        w = st.get("wins", 0)
        row = f"{e(_plain_cat(cat))}: {w} wins of {n}"
        if cat == "unknown":
            cut.append(f"{row} — random unclassified news lost money, budget cut to a trickle")
        elif pm.catalyst_graduated(cat):
            working.append(f"{row} — proven, gets extra budget")
        elif pm.catalyst_dead(cat):
            cut.append(f"{row} — proven loser, cut entirely")
        elif pm.catalyst_explore_slots(cat, base) < base:
            cut.append(f"{row} — losing more than the field, budget cut to a trickle")
        elif n >= 20:
            testing.append(row)
    L.append(f"✅ <b>WORKING ({len(working)})</b>")
    L += [f"• {r}" for r in working[:5]] or ["• nothing proven yet — still earning it"]
    L.append("")
    L.append(f"❌ <b>NOT WORKING → CUT ({len(cut)})</b>")
    L += [f"• {r}" for r in cut[:5]] or ["• nothing on the chopping block today"]
    if testing:
        L.append(f"🧪 still testing: {', '.join(r.split(':')[0] for r in testing[:4])}")
    L.append("")

    # ---- CHANGES it made to itself today ----
    changes = [c for c in (learner.know.get("self_changes") or [])
               if _et_day(c.get("ts", 0)) == today]
    L.append(f"🔧 <b>CHANGES TODAY ({len(changes)})</b>")
    for c in changes[:3]:
        L.append(f"• {e((c.get('title') or 'small self-tune')[:70])}")
    if len(changes) > 3:
        L.append(f"• …and {len(changes) - 3} smaller tunes")
    if not changes:
        L.append("• none — it left itself alone today")
    lab = [r for r in _read_jsonl("state/learning_outcomes.jsonl", 150_000)
           if (r.get("ts") or "")[:10] == today]
    if lab:
        lw = sum(1 for r in lab if r.get("alpha", 0) > 0)
        L.append(f"🧫 practice lab: {len(lab)} experiments finished, {lw} beat the market "
                 f"(these never touch the $500)")
    L.append("")

    # ---- VERDICT ----
    vel = core.velocity(learner.lgate) if getattr(learner, "lgate", None) else {}
    verdict = vel.get("verdict", "TOO EARLY")
    vmap = {
        "ON TRACK": "✅ VERDICT: improving — the score that unlocks real money is climbing. Leave it running.",
        "SPINNING": "⚠️ VERDICT: busy but not improving — if this holds another week, the setup needs rethinking.",
        "TOO EARLY": "⏳ VERDICT: too early to judge — still collecting enough finished trades.",
        "SETTLING": "➖ VERDICT: steady, no clear trend yet — watch the next few days.",
    }
    L.append(vmap.get(verdict, vmap["TOO EARLY"]))
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
