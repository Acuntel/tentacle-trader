"""ROBOTICS SCOUT desk — Brady's personal robotics supply-chain research engine inside TENTACLE.

Folded in 2026-08-07 from the standalone claude.ai cloud routine (repo Acuntel/robotics-scout,
retired the same day). RESEARCH ONLY, by design: this desk never emits signals and never touches
the book, the brain, the patterns, or any ledger the trader learns from. It shares plumbing only:
Google News RSS, the free LLM pool (llm.ask_json), prices, and Telegram (notify.send).

Daily: run the day-of-week rotation from the old protocol, read fresh headlines for the tracked
names, let the free LLM propose evidence-backed score changes against the FIXED 8-criterion
rubric, update the scoreboard + append-only changelog, write a daily log, and send Brady a
SECOND daily Telegram message describing exactly what was researched.

First run on/after the verdict date (2026-09-06): write FINAL_VERDICT.md with the pick, send it,
and make every later run a no-op (the old routine's hard rule #1).
"""
from __future__ import annotations

import os
import time
import urllib.parse
from datetime import date

import feedparser
import requests

from . import llm, market, notify, prices
from .persist import load_json, save_json

DIR = "state/robotics"
BOARD_PATH = f"{DIR}/scoreboard.json"
MARK_PATH = f"{DIR}/last_run.json"
VERDICT_PATH = f"{DIR}/FINAL_VERDICT.md"
LOG_DIR = f"{DIR}/log"
SEED_DAY = "2026-08-07"

UA = os.getenv("TENTACLE_USER_AGENT",
               "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0 Safari/537.36")

# The rubric is FIXED for the full 30 days (old CRITERIA.md). Weights sum to 1.
CRITERIA = ["insourcing-proof", "price-war-proof", "architecture-agnostic", "real dollars today",
            "valuation gap", "content per robot x attach", "US accessibility", "balance sheet & execution"]
WEIGHTS = [0.20, 0.15, 0.15, 0.15, 0.15, 0.10, 0.05, 0.05]

# Seeded 2026-08-07 from the two deep research sessions (old SCOREBOARD.md). Priors, not conclusions.
SEED_BOARD = [
    {"name": "MP Materials", "ticker": "MP", "scores": [10, 8, 10, 6, 7, 4, 10, 6],
     "thesis": "Magnets cannot be insourced; DoD price floor; halved from the high."},
    {"name": "Regal Rexnord", "ticker": "RRX", "scores": [5, 5, 7, 7, 7, 9, 10, 5],
     "thesis": "Only US name with disclosed humanoid dollars; post-reset 15-17x; new CEO plan pending."},
    {"name": "Timken", "ticker": "TKR", "scores": [5, 6, 6, 4, 8, 6, 10, 7],
     "thesis": "US-made harmonic gears (Cone Drive) plus bearings; not priced as robotics at all."},
    {"name": "Novanta", "ticker": "NOVT", "scores": [6, 6, 8, 6, 4, 4, 10, 7],
     "thesis": "ATI force/torque sensors in every wrist; but 41x forward."},
    {"name": "AMETEK", "ticker": "AME", "scores": [5, 7, 6, 5, 5, 4, 10, 9],
     "thesis": "Coreless motors (reported Optimus supplier); fortress balance sheet; diluted exposure."},
    {"name": "THK", "ticker": "6481.T", "scores": [6, 3, 5, 6, 6, 7, 4, 6],
     "thesis": "Roller-screw revenue leader per one report; direct Chinese price-war exposure."},
    {"name": "Moog", "ticker": "MOG-A", "scores": [5, 6, 5, 3, 6, 6, 10, 6],
     "thesis": "Actuation DNA; but current growth is defense and data centers, humanoid unverified."},
    {"name": "Leaderdrive", "ticker": "688017.SS", "scores": [6, 7, 4, 8, 0, 7, 1, 5],
     "thesis": "Winning Optimus reducer share at 100x sales; tracked, not buyable."},
    {"name": "Allient", "ticker": "ALNT", "scores": [4, 4, 5, 4, 6, 5, 10, 5],
     "thesis": "Small-cap motion; JPM upgrade; thin moat evidence so far."},
    {"name": "Harmonic Drive", "ticker": "6324.T", "scores": [7, 3, 4, 7, 1, 7, 4, 6],
     "thesis": "The literal incumbent monopoly, fully priced at roughly 100x forward."},
    {"name": "RBC Bearings", "ticker": "RBC", "scores": [5, 6, 5, 2, 4, 4, 10, 7],
     "thesis": "Samples only, no volume, per its own Q4 FY26 call."},
    {"name": "Nabtesco", "ticker": "6268.T", "scores": [6, 5, 3, 3, 6, 3, 4, 7],
     "thesis": "RV reducers are industrial-robot parts; least humanoid-levered of the Japan trio."},
]

WATCH = ["Tesla Optimus production 2026", "Regal Rexnord humanoid orders", "MP Materials magnet NdPr",
         "Figure robot BotQ suppliers", "planetary roller screw capacity price",
         "NVIDIA robotics Jetson humanoid"]

ROTATION = {  # ET weekday -> (focus title, extra focused queries)
    0: ("news sweep across all tracked names and the humanoid programs",
        ["Tesla Optimus", "Figure AI robot", "Unitree humanoid", "Apptronik", "Agility Robotics",
         "Boston Dynamics Atlas"]),
    1: ("filings and transcripts: earnings, 8-Ks, guidance changes",
        ["Regal Rexnord earnings guidance", "MP Materials earnings", "Timken earnings robotics",
         "Novanta earnings humanoid", "AMETEK earnings"]),
    2: ("price and valuation refresh against criterion 5", ["humanoid robot stocks valuation"]),
    3: ("bottleneck watch: screws, magnets, grinders, Chinese capacity",
        ["planetary roller screw", "NdPr rare earth export controls", "thread grinding machine",
         "Hengli roller screw humanoid", "Zhenyu humanoid actuator", "humanoid actuator price war"]),
    4: ("universe expansion: IPOs, new listings, M&A of private chokepoints",
        ["Unitree IPO STAR", "AgiBot IPO", "EngineAI IPO", "Tesollo KOSDAQ", "robotics supplier acquisition",
         "harmonic reducer maker acquisition"]),
    5: ("red team: attack the current number one and hunt disconfirming evidence", []),
    6: ("weekly synthesis: rank movement, conviction, open questions", ["humanoid robot supply chain"]),
}

SYNTH_SYS = (
    "You are Robotics Scout, a disciplined equity research analyst tracking the humanoid/robotics "
    "supply chain. You score companies on a FIXED 8-criterion rubric (0-10 each): "
    + "; ".join(f"{i + 1}={c} (weight {int(w * 100)}%)" for i, (c, w) in enumerate(zip(CRITERIA, WEIGHTS)))
    + ". Discipline: a score changes ONLY on hard new evidence from today's headlines, never on vibes or "
    "TAM slides. Criterion 4 counts disclosed dollars only. Most days NO score should move. "
    'Reply with JSON only: {"summary": [3-6 short complete sentences, each naming its source headline], '
    '"score_changes": [{"ticker": str, "criterion": 1-8, "new": 0-10, "evidence": str}], '
    '"notable": "one sentence: the single most important thing today"}. '
    "Empty score_changes is the normal, honest answer.")


def _composite(scores: list) -> float:
    return round(sum(s * w for s, w in zip(scores, WEIGHTS)), 2)


def _gnews(q: str) -> str:
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"


class RoboticsDesk:
    """Once-a-day research pass plus its own Telegram send. Isolated from all trading state."""

    def __init__(self, cfg: dict):
        rc = cfg.get("robotics", {})
        self.on = bool(rc.get("enabled", True))
        self.hour_et = int(rc.get("run_hour_et", 8))
        self.verdict_day = str(rc.get("verdict_date", "2026-09-06"))
        self.max_heads = int(rc.get("max_headlines", 60))
        self.st = load_json(MARK_PATH, {"last_day": None, "done": False})

    def due(self) -> bool:
        if not self.on or self.st.get("done"):
            return False
        et = market.now_et()
        return self.st.get("last_day") != et.strftime("%Y-%m-%d") and et.hour >= self.hour_et

    def _board(self) -> dict:
        b = load_json(BOARD_PATH, None)
        if not b:
            b = {"companies": [dict(c, composite=_composite(c["scores"])) for c in SEED_BOARD],
                 "changelog": [{"date": SEED_DAY, "change": "board seeded from the two deep research "
                               "sessions (12 names)", "evidence": "seed log, old Acuntel/robotics-scout repo"}]}
        return b

    def _fetch(self, query: str, tag: str, lookback_days: int) -> list[dict]:
        try:
            r = requests.get(_gnews(query), headers={"User-Agent": UA}, timeout=8)
            if r.status_code != 200:
                return []
            cutoff = time.time() - lookback_days * 86400
            out = []
            for e in feedparser.parse(r.content).entries[:6]:
                ts = time.mktime(e.published_parsed) if getattr(e, "published_parsed", None) else time.time()
                if ts >= cutoff:
                    out.append({"tag": tag, "title": getattr(e, "title", "")[:150],
                                "when": time.strftime("%m-%d", time.localtime(ts))})
            return out
        except Exception:
            return []

    def _gather(self, board: dict, weekday: int) -> list[dict]:
        focus_qs = list(ROTATION[weekday][1])
        if weekday == 5 and board["companies"]:                       # red team hits the leader
            top = max(board["companies"], key=lambda c: c["composite"])
            focus_qs = [f'"{top["name"]}" risk OR bear OR short OR delay OR problem']
        heads, seen = [], set()
        lookback = 7 if weekday in (0, 6) else 3
        for c in board["companies"]:
            heads += self._fetch(f'"{c["name"]}" robotics OR humanoid OR stock', c["ticker"], lookback)
        for q in focus_qs + WATCH[:3]:
            heads += self._fetch(q, "focus", lookback)
        out = []
        for h in heads:                                               # dedupe echoed copies by title
            k = h["title"].lower()[:80]
            if k not in seen:
                seen.add(k)
                out.append(h)
        return out[:self.max_heads]

    def _prices(self, board: dict) -> list[str]:
        """Wednesday criterion-5 input refresh for the US-listed names; notes moves over 5%."""
        notes = []
        for c in board["companies"]:
            if c["scores"][6] < 7:                                    # only cleanly US-accessible names
                continue
            px = prices.last_price(c["ticker"])
            if not px:
                continue
            prev = c.get("last_px")
            c["last_px"] = round(px, 2)
            if prev:
                mv = (px / prev - 1) * 100
                if abs(mv) >= 5:
                    notes.append(f"{c['ticker']} moved {mv:+.1f}% since the last check (now ${px:,.2f}).")
        return notes

    def _apply(self, board: dict, changes: list, today: str) -> list[str]:
        lines = []
        by_tkr = {c["ticker"]: c for c in board["companies"]}
        for ch in (changes or [])[:4]:                                # scoring discipline: max 4 moves/day
            c = by_tkr.get(str(ch.get("ticker", "")).upper())
            try:
                i, new = int(ch["criterion"]) - 1, max(0, min(10, int(ch["new"])))
                ev = str(ch.get("evidence", "")).strip()
            except Exception:
                continue
            if not c or not ev or not 0 <= i < 8 or c["scores"][i] == new:
                continue
            old = c["scores"][i]
            c["scores"][i] = new
            c["composite"] = _composite(c["scores"])
            board["changelog"].append({"date": today, "change": f"{c['ticker']} C{i + 1} {old}->{new}",
                                       "evidence": ev[:200]})
            lines.append(f"{c['ticker']} criterion {i + 1} ({CRITERIA[i]}): {old} to {new}. {ev[:160]}")
        return lines

    def _log_md(self, today: str, focus: str, heads: list, summary: list, changed: list, notable: str):
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(f"{LOG_DIR}/{today}.md", "w") as f:
            f.write(f"# Robotics Scout — {today}\n\nFocus: {focus}\nHeadlines read: {len(heads)}\n\n"
                    + (f"Notable: {notable}\n\n" if notable else "")
                    + "## Findings\n" + "".join(f"- {s}\n" for s in summary)
                    + "\n## Score changes\n" + ("".join(f"- {c}\n" for c in changed) or "- none\n")
                    + "\n## Headlines\n" + "".join(f"- [{h['tag']} {h['when']}] {h['title']}\n" for h in heads))

    def run(self) -> str:
        """One daily pass. Marks the day first so a bad day fails once, not every 15s tick."""
        today = market.now_et().strftime("%Y-%m-%d")
        self.st["last_day"] = today
        save_json(MARK_PATH, self.st)
        if today >= self.verdict_day or os.path.exists(VERDICT_PATH):
            return self._verdict(today)
        board = self._board()
        weekday = market.now_et().weekday()
        focus = ROTATION[weekday][0]
        heads = self._gather(board, weekday)
        px_notes = self._prices(board) if weekday == 2 else []
        res = llm.ask_json(SYNTH_SYS, "Today's focus: " + focus + "\n\nCurrent leaderboard:\n"
                           + "\n".join(f"{c['ticker']} ({c['name']}) composite {c['composite']}: "
                                       + ",".join(map(str, c["scores"])) for c in board["companies"])
                           + "\n\nToday's fresh headlines:\n"
                           + "\n".join(f"[{h['tag']} {h['when']}] {h['title']}" for h in heads)
                           + ("\n\nPrice notes:\n" + "\n".join(px_notes) if px_notes else ""),
                           max_tokens=1200, task="reason") or {}
        summary = [str(s)[:220] for s in (res.get("summary") or [])][:6] or \
            [f"[{h['tag']}] {h['title']}" for h in heads[:5]] or \
            ["No fresh headlines cleared the recency filter today."]
        changed = self._apply(board, res.get("score_changes"), today)
        notable = str(res.get("notable", ""))[:220]
        board["companies"].sort(key=lambda c: -c["composite"])
        save_json(BOARD_PATH, board)
        self._log_md(today, focus, heads, summary, changed, notable)
        self._send_daily(today, focus, heads, summary, changed, notable, board)
        return f"researched {len(heads)} headlines ({focus.split(':')[0]}), {len(changed)} score change(s)"

    def _send_daily(self, today, focus, heads, summary, changed, notable, board):
        e = notify.esc
        day_n = (date.fromisoformat(today) - date.fromisoformat(SEED_DAY)).days
        left = (date.fromisoformat(self.verdict_day) - date.fromisoformat(today)).days
        L = [f"🤖 <b>ROBOTICS SCOUT · daily research · {market.now_et().strftime('%a %b %-d')}</b>",
             f"Day {day_n} of 30. Today's focus: {e(focus)}.",
             f"I read {len(heads)} fresh headlines across the tracked names.", ""]
        if notable:
            L += [f"⭐ {e(notable)}", ""]
        L.append("<b>WHAT I FOUND</b>")
        L += [f"• {e(s)}" for s in summary]
        L += ["", "<b>SCORE CHANGES</b>"]
        L += [f"• {e(c)}" for c in changed] or ["• No score moved today. The rubric only moves on hard evidence."]
        L += ["", "<b>LEADERBOARD</b>"]
        L += [f"{i + 1}. {e(c['name'])} ({e(c['ticker'])}): {c['composite']}"
              for i, c in enumerate(board["companies"][:5])]
        L += ["", f"Final pick lands {self.verdict_day} ({left} days). This desk is research only: "
                  "TENTACLE does not trade these names for you."]
        notify.send("\n".join(L)[:3900])

    def _verdict(self, today: str) -> str:
        if self.st.get("done"):
            return "verdict already delivered; desk idle"
        board = self._board()
        buyable = [c for c in board["companies"] if c["scores"][6] >= 7] or board["companies"]
        buyable.sort(key=lambda c: -c["composite"])
        pick, runner = buyable[0], (buyable[1] if len(buyable) > 1 else None)
        chain = [r for r in board["changelog"] if r["date"] != SEED_DAY][-12:]
        case = llm.ask(
            "You are Robotics Scout delivering a 30-day final verdict. Write plainly, complete sentences, "
            "no hedging filler. Sections: the case for the pick (one paragraph), why the runner-up lost, "
            "entry logic, and 3 invalidation criteria (specific, measurable).",
            f"Pick: {pick['name']} ({pick['ticker']}), composite {pick['composite']}. Thesis: {pick['thesis']}\n"
            + (f"Runner-up: {runner['name']} ({runner['ticker']}), composite {runner['composite']}. "
               f"Thesis: {runner['thesis']}\n" if runner else "")
            + "Evidence chain from the 30 days:\n"
            + "\n".join(f"- {r['date']}: {r['change']} ({r['evidence']})" for r in chain),
            max_tokens=900) or (
            f"The scoreboard's weighted rubric ranks {pick['name']} first at {pick['composite']}. "
            f"Thesis: {pick['thesis']} "
            + (f"Runner-up {runner['name']} at {runner['composite']}: {runner['thesis']}" if runner else ""))
        os.makedirs(DIR, exist_ok=True)
        with open(VERDICT_PATH, "w") as f:
            f.write(f"# FINAL VERDICT — {today}\n\n**Pick: {pick['name']} ({pick['ticker']})**, "
                    f"composite {pick['composite']}/10.\n\n{case}\n\n## Evidence chain\n"
                    + ("".join(f"- {r['date']}: {r['change']} ({r['evidence']})\n" for r in chain)
                       or "- No score-moving evidence landed during the run; the seed thesis held.\n"))
        e = notify.esc
        notify.send((f"🏁 <b>ROBOTICS SCOUT · 30-DAY FINAL VERDICT</b>\n\n"
                     f"The pick is <b>{e(pick['name'])} ({e(pick['ticker'])})</b>, "
                     f"final score {pick['composite']}/10.\n\n{e(case)}\n\n"
                     "The full verdict with the evidence chain is saved on the server at "
                     "state/robotics/FINAL_VERDICT.md. This was research only: nothing was traded.")[:3900])
        self.st["done"] = True
        save_json(MARK_PATH, self.st)
        return f"FINAL VERDICT delivered: {pick['name']} ({pick['ticker']}) at {pick['composite']}"


if __name__ == "__main__":
    # Preview or force a run: `python -m earshot.robotics` composes and sends today's research pass
    # (inside the container via railway ssh to use the live volume). Bypasses the once-a-day gate.
    import yaml
    d = RoboticsDesk(yaml.safe_load(open("config.yaml")))
    d.st = {"last_day": None, "done": os.path.exists(VERDICT_PATH) and d.st.get("done", False)}
    print(d.run())
