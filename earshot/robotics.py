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

import json
import os
import time
import urllib.parse
from datetime import date

import feedparser
import requests

from . import llm, market, notify, prices
from .persist import load_json, save_json
from .robotics_feed import RoboticsFeed, _tier, flush_pending, queue_send

DIR = "state/robotics"
BOARD_PATH = f"{DIR}/scoreboard.json"
MARK_PATH = f"{DIR}/last_run.json"
MEM_PATH = f"{DIR}/memory.json"
FUND_PATH = f"{DIR}/fundamentals.json"
VERDICT_PATH = f"{DIR}/FINAL_VERDICT.md"
LOG_DIR = f"{DIR}/log"
SEED_DAY = "2026-08-07"

SEED_QUESTIONS = [
    "Does Optimus actually ramp toward 50k+ units in 2026?",
    "Does RRX's humanoid order line recover from $1M, and what does Aamir Paul's strategy refresh change?",
    "Does MP hit magnet output milestones, and do new offtakes land beyond DoD/GM?",
    "Does Chinese screw capacity confirm a price war (falling ASPs) or does demand absorb it?",
    "Does any listed industrial acquire a grinder or screw maker?",
    "What do Unitree's aftermarket and the AgiBot/EngineAI listings imply for component multiples?",
    "Does a US-accessible pure-play listing emerge?",
]

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

_RUBRIC = "; ".join(f"{i + 1}={c} (weight {int(w * 100)}%)"
                    for i, (c, w) in enumerate(zip(CRITERIA, WEIGHTS)))
_SCHEMA = (
    'Reply with JSON only: {"summary": [3-6 short complete sentences, each naming its source, ONLY '
    "genuinely NEW information not in the previously-reported list], "
    '"score_changes": [{"ticker": str, "criterion": 1-8, "new": 0-10, "evidence": str}], '
    '"add_company": null OR {"name": str, "ticker": str, "scores": [8 ints 0-10], "thesis": str, '
    '"evidence": str} when T1/T2 evidence reveals ONE publicly listed company that genuinely belongs '
    "on the scoreboard and is not on it (score all 8 criteria honestly; null almost every day), "
    '"retire": null OR {"ticker": str, "reason": str} for ONE name that clearly no longer belongs, '
    '"open_questions": [the 3-7 most important open questions going forward, carried over and updated], '
    '"notable": "one sentence: the single most important NEW thing today"}. '
    "Empty score_changes is the normal, honest answer.")
_TIER_RULE = (
    "Items are tagged [tag/T1|T2|T3 date]: T1 = SEC filings and company press releases (primary), "
    "T2 = reputable press, T3 = blogs/aggregators. Criterion 4 (real dollars) may only move on T1 "
    "evidence; every other criterion needs T1 or T2. T3 never moves a score. ")

SYNTH_SYS = (
    "You are Robotics Scout, a disciplined equity research analyst tracking the humanoid/robotics "
    "supply chain. You score companies on a FIXED 8-criterion rubric (0-10 each): " + _RUBRIC
    + ". Discipline: a score changes ONLY on hard new evidence, never on vibes or TAM slides. "
    + _TIER_RULE + "Never re-report a finding from the previously-reported list; if nothing genuinely "
    "new happened for a name, say nothing about it. " + _SCHEMA)

RED_SYS = (
    "You are Robotics Scout on RED TEAM day. Your only job today is to attack the current leader on "
    "the scoreboard: hunt for disconfirming evidence in the items provided, steelman the bear case, "
    "and state what would have to be true for the leader to fail and whether any of it is showing up. "
    "Rubric: " + _RUBRIC + ". " + _TIER_RULE
    + 'Reply with JSON only: {"summary": [3-5 complete sentences of the strongest DISCONFIRMING '
    'evidence found, each naming its source], "bear_case": "one honest paragraph: the strongest case '
    'against the leader today", "score_changes": [{"ticker": str, "criterion": 1-8, "new": 0-10, '
    '"evidence": str}], "open_questions": [3-7 questions], "notable": "one sentence"}. '
    "If the bear hunt comes up empty, say so plainly in bear_case.")


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
        self.feed = RoboticsFeed(cfg, UA)                      # the 24/7 collector

    def due(self) -> bool:
        if not self.on or self.st.get("done"):
            return False
        et = market.now_et()
        return self.st.get("last_day") != et.strftime("%Y-%m-%d") and et.hour >= self.hour_et

    def upkeep(self):
        """Called every main-loop cycle: run the 24/7 collector when a batch is due and retry any
        queued Telegram send. Must never throw and never block the trading loop meaningfully."""
        if not self.on:
            return
        try:
            if not self.st.get("done") and self.feed.due():
                fresh = self.feed.tick()
                if fresh:
                    print(f"  [robotics-feed] +{fresh} new item(s) in the corpus")
            if flush_pending(notify.send):
                print("  [robotics] queued Telegram delivered")
        except Exception as ex:
            print(f"  [robotics-feed] tick failed: {ex}")

    def _memory(self) -> dict:
        return load_json(MEM_PATH, {"days": [], "open_questions": list(SEED_QUESTIONS), "bear": {}})

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
                    title = getattr(e, "title", "")[:170]
                    src = title.rsplit(" - ", 1)[-1] if " - " in title else ""
                    out.append({"tag": tag, "title": title, "tier": _tier(src),
                                "when": time.strftime("%m-%d", time.localtime(ts))})
            return out
        except Exception:
            return []

    def _gather(self, board: dict, weekday: int) -> list[dict]:
        """Primary material = whatever the 24/7 collector already gathered; live queries only add
        depth on today's focus. Falls back to the old direct sweep when the corpus is cold."""
        heads = [{"tag": it.get("tag", "?"), "title": it.get("title", ""), "tier": it.get("tier", 3),
                  "when": time.strftime("%m-%d", time.localtime(it.get("ts", time.time()))),
                  "body": it.get("body", "")} for it in self.feed.recent()]
        focus_qs = list(ROTATION[weekday][1])
        if weekday == 5 and board["companies"]:                       # red team hits the leader
            top = max(board["companies"], key=lambda c: c["composite"])
            focus_qs = [f'"{top["name"]}" risk OR bear OR short OR delay OR problem',
                        f'"{top["name"]}" competition OR insourcing OR price war']
        for q in focus_qs:
            heads += self._fetch(q, "focus", 3)
        if len(heads) < 10:                                           # cold corpus (fresh volume/boot)
            for c in board["companies"]:
                heads += self._fetch(f'"{c["name"]}" robotics OR humanoid OR stock', c["ticker"], 3)
        seen, out = set(), []
        for h in sorted(heads, key=lambda h: (h.get("tier", 3), -len(h.get("body", "")))):
            k = h["title"].lower()[:80]                               # best tier + full-text first
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

    def _fundamentals(self, board: dict) -> list[str]:
        """Daily-cached valuation snapshot for every scoreboard name (criterion 5 stops running on
        the seed's memory of multiples). Works for the .T/.SS names too via yfinance."""
        cache = load_json(FUND_PATH, {"ts": 0, "rows": {}})
        if time.time() - cache.get("ts", 0) > 20 * 3600:
            rows = {}
            try:
                import yfinance as yf
                for c in board["companies"]:
                    try:
                        info = yf.Ticker(c["ticker"]).info or {}
                        px = info.get("currentPrice") or info.get("regularMarketPrice")
                        hi = info.get("fiftyTwoWeekHigh")
                        rows[c["ticker"]] = {
                            "fpe": info.get("forwardPE"), "ps": info.get("priceToSalesTrailing12Months"),
                            "off_high": round((1 - px / hi) * 100, 1) if px and hi else None,
                            "short": info.get("shortPercentOfFloat"),
                            "tgt": info.get("targetMeanPrice"), "px": px}
                    except Exception:
                        continue
            except Exception:
                pass
            if rows:
                cache = {"ts": time.time(), "rows": rows}
                save_json(FUND_PATH, cache)
        lines = []
        for t, r in cache.get("rows", {}).items():
            bits = []
            if r.get("fpe"):
                bits.append(f"fwd P/E {r['fpe']:.0f}")
            if r.get("ps"):
                bits.append(f"P/S {r['ps']:.1f}")
            if r.get("off_high") is not None:
                bits.append(f"{r['off_high']:.0f}% off 52wk high")
            if r.get("short"):
                bits.append(f"short {r['short'] * 100:.0f}% of float")
            if r.get("px") and r.get("tgt"):
                bits.append(f"analyst target {(r['tgt'] / r['px'] - 1) * 100:+.0f}%")
            if bits:
                lines.append(f"{t}: " + ", ".join(bits))
        return lines

    def _verify(self, changes: list, heads: list) -> list:
        """Second-opinion pass: every proposed change survives only if an independent skeptical call
        fails to refute it. A None verdict (verifier unavailable) keeps the change — one model with
        keys must not brick the desk; an explicit confirmed:false kills it."""
        out = []
        ev = "\n".join(f"[{h['tag']}/T{h.get('tier', 3)}] {h['title']}" for h in heads[:40])
        for ch in (changes or [])[:5]:
            v = llm.ask_json(
                "You are a skeptical research fact-checker for a robotics supply-chain scoreboard. "
                "Decide if the cited evidence really supports the proposed change under these rules: "
                "criterion 4 (real disclosed dollars) requires a tier-1 item (SEC filing or company "
                "press release); other criteria need tier 1 or 2; narrative, TAM talk, or a price move "
                'alone never justifies a change. Reply with JSON only: {"confirmed": true or false, '
                '"why": "one sentence"}.',
                "Proposed change: " + json.dumps(ch) + "\n\nToday's evidence items:\n" + ev,
                max_tokens=200, temperature=0.0, task="reason")
            if v is not None and v.get("confirmed") is False:
                print(f"  [robotics] verifier refuted a change ({str(ch.get('ticker', '?'))}): "
                      f"{str(v.get('why', ''))[:90]}")
                continue
            out.append(ch)
        return out

    def _mutate_board(self, board: dict, add, retire, today: str) -> list[str]:
        """The scoreboard is alive: verified discoveries join it, dead names leave it. Bounded to one
        add and one retire per day; the top 5 and a minimum board of 8 are untouchable."""
        notes = []
        if isinstance(add, dict) and add.get("ticker") and str(add.get("evidence", "")).strip():
            t = str(add["ticker"]).upper()
            try:
                scores = [max(0, min(10, int(s))) for s in (add.get("scores") or [])][:8]
                if (len(scores) == 8 and len(board["companies"]) < 18
                        and t not in {c["ticker"] for c in board["companies"]}):
                    row = {"name": str(add.get("name", t))[:60], "ticker": t, "scores": scores,
                           "composite": _composite(scores), "thesis": str(add.get("thesis", ""))[:160]}
                    board["companies"].append(row)
                    board["changelog"].append({"date": today, "change": f"ADDED {t} at {row['composite']}",
                                               "evidence": str(add["evidence"])[:200]})
                    notes.append(f"New name on the scoreboard: {row['name']} ({t}) enters at "
                                 f"{row['composite']}. {str(add['evidence'])[:120]}")
            except Exception:
                pass
        if isinstance(retire, dict) and retire.get("ticker"):
            t = str(retire["ticker"]).upper()
            comps = board["companies"]
            top5 = {c["ticker"] for c in sorted(comps, key=lambda c: -c["composite"])[:5]}
            tgt = next((c for c in comps if c["ticker"] == t), None)
            if tgt and t not in top5 and len(comps) > 8:
                comps.remove(tgt)
                board.setdefault("retired", []).append(
                    dict(tgt, retired=today, reason=str(retire.get("reason", ""))[:160]))
                board["changelog"].append({"date": today, "change": f"RETIRED {t}",
                                           "evidence": str(retire.get("reason", ""))[:200]})
                notes.append(f"Retired {tgt['name']} ({t}) from the scoreboard. "
                             f"{str(retire.get('reason', ''))[:120]}")
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
        mem = self._memory()
        weekday = market.now_et().weekday()
        focus = ROTATION[weekday][0]
        heads = self._gather(board, weekday)
        px_notes = self._prices(board) if weekday == 2 else []
        fund = self._fundamentals(board)
        bodies = [h for h in heads if h.get("body")][:6]
        prev = [f"({d['date']}) {f}" for d in mem["days"][-3:] for f in [d.get("notable", "")] + d.get("findings", []) if f]
        user = ("Today's focus: " + focus + "\n\nCurrent leaderboard:\n"
                + "\n".join(f"{c['ticker']} ({c['name']}) composite {c['composite']}: "
                            + ",".join(map(str, c["scores"])) for c in board["companies"])
                + "\n\nOpen questions being tracked:\n" + "\n".join(f"- {q}" for q in mem["open_questions"])
                + ("\n\nPreviously reported (do NOT repeat):\n" + "\n".join(f"- {p}" for p in prev[:18]) if prev else "")
                + "\n\nFresh items from the 24/7 collector:\n"
                + "\n".join(f"[{h['tag']}/T{h.get('tier', 3)} {h['when']}] {h['title']}" for h in heads)
                + ("\n\nFull-article excerpts:\n" + "\n\n".join(
                    f"[{b['tag']}] {b['title']}\n{b['body'][:700]}" for b in bodies) if bodies else "")
                + ("\n\nValuation snapshot (criterion 5 inputs, refreshed daily):\n"
                   + "\n".join(fund) if fund else "")
                + ("\n\nPrice notes:\n" + "\n".join(px_notes) if px_notes else ""))
        res = llm.ask_json(RED_SYS if weekday == 5 else SYNTH_SYS, user,
                           max_tokens=1400, task="reason") or {}
        summary = [str(s)[:220] for s in (res.get("summary") or [])][:6] or \
            [f"[{h['tag']}] {h['title']}" for h in heads[:5]] or \
            ["No fresh headlines cleared the recency filter today."]
        changed = self._apply(board, self._verify(res.get("score_changes"), heads), today)
        add = res.get("add_company")
        add_ok = (self._verify([add], heads) or [None])[0] if isinstance(add, dict) else None
        changed += self._mutate_board(board, add_ok, res.get("retire"), today)
        notable = str(res.get("notable", ""))[:220]
        bear = str(res.get("bear_case", ""))[:500] if weekday == 5 else ""
        board["companies"].sort(key=lambda c: -c["composite"])
        save_json(BOARD_PATH, board)
        oq = [str(q)[:160] for q in (res.get("open_questions") or []) if str(q).strip()][:7]
        mem["days"] = (mem["days"] + [{"date": today, "notable": notable, "findings": summary}])[-7:]
        if oq:
            mem["open_questions"] = oq
        if bear:
            mem["bear"] = {"date": today, "case": bear}
        save_json(MEM_PATH, mem)
        self._log_md(today, focus, heads, summary, changed, notable)
        self._send_daily(today, focus, heads, summary, changed, notable, board, bear, len(bodies))
        return (f"researched {len(heads)} items ({focus.split(':')[0]}), {len(bodies)} full reads, "
                f"{len(changed)} score change(s)")

    def _send_daily(self, today, focus, heads, summary, changed, notable, board, bear="", n_reads=0):
        e = notify.esc
        day_n = (date.fromisoformat(today) - date.fromisoformat(SEED_DAY)).days
        left = (date.fromisoformat(self.verdict_day) - date.fromisoformat(today)).days
        last24 = self.feed.recent(24)
        n_edgar = sum(1 for it in last24 if it.get("tag") == "edgar")
        L = [f"🤖 <b>ROBOTICS SCOUT · daily research · {market.now_et().strftime('%a %b %-d')}</b>",
             f"Day {day_n} of 30. Today's focus: {e(focus)}.",
             f"The 24/7 collector gathered {len(last24)} new items in the last 24 hours "
             f"({n_edgar} SEC filings). I analyzed {len(heads)} and read {n_reads} articles in full.", ""]
        if notable:
            L += [f"⭐ {e(notable)}", ""]
        L.append("<b>WHAT I FOUND (new only)</b>")
        L += [f"• {e(s)}" for s in summary]
        if bear:
            L += ["", "<b>RED TEAM: THE BEAR CASE ON THE LEADER</b>", e(bear)]
        L += ["", "<b>SCORE & BOARD CHANGES</b>"]
        L += [f"• {e(c)}" for c in changed] or ["• Nothing moved today. Scores and the board only "
                                                "move on verified primary-source evidence."]
        L += ["", f"<b>LEADERBOARD (top 5 of {len(board['companies'])})</b>"]
        L += [f"{i + 1}. {e(c['name'])} ({e(c['ticker'])}): {c['composite']}"
              for i, c in enumerate(board["companies"][:5])]
        L += ["", f"Final pick lands {self.verdict_day} ({left} days). This desk is research only: "
                  "TENTACLE does not trade these names for you."]
        msg = "\n".join(L)[:3900]
        if not notify.send(msg):
            queue_send(msg)
            print("  [robotics] Telegram send failed — queued, will retry every 2 minutes")

    def _verdict(self, today: str) -> str:
        if self.st.get("done"):
            return "verdict already delivered; desk idle"
        board = self._board()
        buyable = [c for c in board["companies"] if c["scores"][6] >= 7] or board["companies"]
        buyable.sort(key=lambda c: -c["composite"])
        pick, runner = buyable[0], (buyable[1] if len(buyable) > 1 else None)
        chain = [r for r in board["changelog"] if r["date"] != SEED_DAY][-12:]
        mem = self._memory()
        case = llm.ask(
            "You are Robotics Scout delivering a 30-day final verdict. Write plainly, complete sentences, "
            "no hedging filler. Sections: the case for the pick (one paragraph), why the runner-up lost, "
            "the strongest bear case and your answer to it, entry logic, and 3 invalidation criteria "
            "(specific, measurable).",
            f"Pick: {pick['name']} ({pick['ticker']}), composite {pick['composite']}. Thesis: {pick['thesis']}\n"
            + (f"Runner-up: {runner['name']} ({runner['ticker']}), composite {runner['composite']}. "
               f"Thesis: {runner['thesis']}\n" if runner else "")
            + (f"Latest red-team bear case ({mem['bear'].get('date', '?')}): {mem['bear'].get('case', '')}\n"
               if mem.get("bear", {}).get("case") else "")
            + "Open questions at the end of the run:\n"
            + "\n".join(f"- {q}" for q in mem.get("open_questions", [])) + "\n"
            + "Evidence chain from the 30 days:\n"
            + "\n".join(f"- {r['date']}: {r['change']} ({r['evidence']})" for r in chain),
            max_tokens=1000) or (
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
        vmsg = (f"🏁 <b>ROBOTICS SCOUT · 30-DAY FINAL VERDICT</b>\n\n"
                f"The pick is <b>{e(pick['name'])} ({e(pick['ticker'])})</b>, "
                f"final score {pick['composite']}/10.\n\n{e(case)}\n\n"
                "The full verdict with the evidence chain is saved on the server at "
                "state/robotics/FINAL_VERDICT.md. This was research only: nothing was traded.")[:3900]
        if not notify.send(vmsg):
            queue_send(vmsg)
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
