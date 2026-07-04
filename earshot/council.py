"""The Hive — TENTACLE's 24/7 recursive multi-model agent council.

The free models act as specialized AGENTS that improve the bot together and CHECK EACH OTHER'S WORK,
fed by a persistent task queue the agents themselves keep refilling — "agents spinning up agents",
bounded so it can never run away or stall the trading loop.

Roles (each routed to a DIFFERENT model via task lanes, so no model grades its own homework):
  COORDINATOR (reason)  — reads the whole brain + recent results, ENQUEUES the next batch of work
  CODER       (code)    — writes an actual patch (find/replace over earshot/*.py)
  REVIEWER    (reason)  — a DIFFERENT model adversarially critiques the coder's patch (real? safe? buggy?)
  JUDGE       (judge)   — fast final veto
  STRATEGIST  (quant)   — designs concrete, testable trading strategies
  SCOUT       (coord.)  — surfaces new data sources / tools the system wants -> Telegram

Everything vetted flows into the EXISTING pipelines: code -> proposer -> ✅/❌ Telegram card -> CI
test-gate on approval. The money container NEVER writes its own code here — the Hive only PROPOSES and
cross-reviews (LLM calls + read-only file checks). The queue + memory live in knowledge.json on the
Railway volume (atomic, daily-backed-up) so context is never lost across restarts.

Bounds (free-tier + safety): max_tasks_per_tick, max_queue, max_seed, dedupe vs recent. Unbounded agent
spawning would instantly exhaust the free quotas and is a cost-runaway footgun — this is the safe version.
The 3 rails (risk caps / money gate / Gauntlet) are never editable.
"""
from __future__ import annotations

import json
import os
import time

from . import llm
from .models import iso

EVO_DIR = "state/evolution"
HIVE_LOG = os.path.join(EVO_DIR, "hive.jsonl")
PKG = os.path.dirname(__file__)
LOCKED = {"risk.py", "executor.py", "backtest.py"}        # the rails — never self-edit

COORD_SYS = (
    "You are the COORDINATOR of an autonomous (still PRACTICE-mode) trading bot's self-improvement hive. "
    "Given the bot's brain, its recent work, a BACKLOG of past ideas worth reviving, past LESSONS/mistakes, "
    "and code that FAILED the gate, choose the few HIGHEST-VALUE next tasks toward consistent risk-adjusted "
    "returns. REVIVE valuable past ideas from the backlog (don't let old work rot), RETRY worthwhile failed "
    "code properly, and DO NOT repeat past mistakes. Reply ONLY JSON: {\"tasks\":[{\"kind\":\"improve_code|"
    "find_source|request_tool|design_strategy\",\"spec\":\"<for improve_code: ONE earshot/*.py filename to "
    "harden; otherwise a short concrete description>\",\"why\":\"<evidence-based reason, cite the data>\","
    "\"priority\":0-100}]}. Be concrete, safe, non-duplicative. You may NOT touch risk caps, the money gate, "
    "or the Gauntlet.")
CODER_SYS = (
    "You improve an autonomous trading bot's own Python source. Propose ONE small, safe, concrete change "
    "to the given file that fixes a bug or measurably improves it. You may NOT weaken the hard risk caps, "
    "the real-money gate, or the Gauntlet. HOT-PATH RULE: only patch code that actually EXECUTES — in "
    "`why`, name the caller that invokes what you changed. NEVER add or 'improve' a helper nothing calls "
    "(a past self-change created a dead gate_score() and the loop wasted 7+ patches polishing it); if you "
    "find dead code, propose DELETING it. Reply ONLY JSON: {\"find\":\"<exact snippet present once in the "
    "file>\",\"replace\":\"<the improved snippet>\",\"why\":\"<one sentence incl. the call site>\"}. Keep "
    "`find` short and unique; `replace` must be valid Python.")
REVIEW_SYS = (
    "You are a SKEPTICAL senior engineer reviewing ANOTHER model's patch to a LIVE trading bot. Reject it "
    "if it could break the loop, changes behavior unsafely, touches a risk rail, or is speculative / not "
    "clearly justified. KILL any patch to code with no caller (dead code) or whose `why` does not name a "
    "real call site — 'survives tests' is not 'improves the bot'. Only keep changes you are confident are "
    "correct, safe, and actually reachable at runtime. Reply ONLY JSON: "
    "{\"verdict\":\"keep|kill\",\"issues\":[\"...\"],\"confidence\":0-100}.")
JUDGE_SYS = ("Final gate on a code change for a trading bot, given the patch and a reviewer's critique. "
             "Reply ONLY JSON: {\"ship\":true|false,\"why\":\"<short>\"}.")
STRAT_SYS = (
    "You are a quant strategist for a long-only equities bot on free data. Given the idea and the bot's "
    "brain, specify ONE concrete, testable strategy. Reply ONLY JSON: {\"name\":\"...\",\"entry\":\"...\","
    "\"exit\":\"...\",\"hold_days\":N,\"families\":[\"...\"],\"rationale\":\"...\"}. Be realistic; no "
    "promises of guaranteed profit.")


def _json(txt):
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        try:
            s, e = txt.index("{"), txt.rindex("}")
            return json.loads(txt[s:e + 1])
        except Exception:
            return None


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Hive:
    def __init__(self, cfg, know, approvals=None, proposer=None, persist=None):
        h = cfg.get("hive", {})
        self.cfg = cfg
        self.on = bool(h.get("enabled", True))
        self.every = int(h.get("every_minutes", 30)) * 60
        self.max_per_tick = int(h.get("max_tasks_per_tick", 1))
        self.max_queue = int(h.get("max_queue", 40))
        self.seed_below = int(h.get("seed_when_below", 3))
        self.max_seed = int(h.get("max_seed", 5))
        self.code_impact = int(h.get("code_impact", 62))
        self.know = know
        self.approvals = approvals
        self.proposer = proposer
        self.persist = persist or (lambda: None)
        self.hv = know.setdefault("hive", {})
        self.q = self.hv.setdefault("queue", [])
        self.hv.setdefault("done", [])
        self.hv.setdefault("strategies", [])
        self.hv.setdefault("next_id", 1)
        self.last = 0.0

    def due(self) -> bool:
        return self.on and (time.time() - self.last) >= self.every

    # ---- queue ----
    def _enqueue(self, kind, spec, why="", prio=50, parent=None) -> bool:
        if len(self.q) >= self.max_queue:
            return False
        h = f"{kind}:{spec}"
        if any(t.get("hash") == h for t in self.q):
            return False
        if any(t.get("hash") == h for t in self.hv["done"][-60:]):   # don't redo recent work
            return False
        tid = f"H{self.hv['next_id']:04d}"
        self.hv["next_id"] += 1
        self.q.append({"id": tid, "hash": h, "kind": kind, "spec": spec, "why": why,
                       "prio": prio, "parent": parent, "ts": iso(time.time())})
        return True

    def _pop(self):
        if not self.q:
            return None
        self.q.sort(key=lambda t: (-int(t.get("prio", 50)), t.get("ts", "")))
        return self.q.pop(0)

    def _finish(self, task, result):
        rec = {**task, "result": result, "done_ts": iso(time.time())}
        self.hv["done"].append(rec)
        self.hv["done"] = self.hv["done"][-200:]
        _append(HIVE_LOG, rec)

    # ---- the 24/7 tick (called from the coach; never raises) ----
    def tick(self, snapshot) -> dict:
        if not llm.have_free_brain():
            return {"skipped": "no free LLM"}
        self.last = time.time()
        try:
            seeded = 0
            if len(self.q) < self.seed_below:
                seeded = self._coordinate(snapshot)           # COORDINATOR spins up the next agents/tasks
            n = 0
            while n < self.max_per_tick and self.q:
                self._dispatch(self._pop(), snapshot)
                n += 1
            self.persist()
            return {"seeded": seeded, "processed": n, "queued": len(self.q)}
        except Exception as e:
            return {"error": str(e)[:140]}

    def _dispatch(self, task, snapshot):
        if not task:
            return
        try:
            k = task["kind"]
            if k == "improve_code":
                r = self._improve_code(task)
            elif k == "design_strategy":
                r = self._design_strategy(task, snapshot)
            elif k in ("find_source", "request_tool"):
                r = self._tool_or_source(task)
            else:
                r = {"status": "unknown_kind"}
        except Exception as e:
            r = {"status": "error", "why": str(e)[:140]}
        self._finish(task, r)

    # ---- COORDINATOR: agents spinning up agents ----
    def _coordinate(self, snapshot) -> int:
        from . import recall
        ctx = ("Brain snapshot:\n" + json.dumps(snapshot, default=str)[:3000] +
               "\n\nBACKLOG — past ideas to REVIVE (give old work utility):\n"
               + json.dumps(recall.backlog(self.know, 6), default=str)[:1200] +
               "\n\nLESSONS / past mistakes (do NOT repeat):\n"
               + json.dumps(recall.lessons(self.know, 5), default=str)[:600] +
               "\n\nCode that FAILED the gate (retry properly if worthwhile):\n"
               + json.dumps(recall.reverted(2), default=str)[:500] +
               "\n\nRecent work (avoid duplicating):\n" + json.dumps(self.hv["done"][-6:], default=str)[:700])
        v = llm.ask_json(COORD_SYS, ctx, max_tokens=750, priority="normal", task="reason")
        added = 0
        for t in ((v or {}).get("tasks") or [])[:self.max_seed]:
            kind = str(t.get("kind", "")).strip()
            spec = str(t.get("spec", "")).strip()[:200]
            if kind in ("improve_code", "find_source", "request_tool", "design_strategy") and spec:
                p = t.get("priority", 50)
                prio = int(p) if str(p).isdigit() else 50
                if self._enqueue(kind, spec, why=str(t.get("why", ""))[:200], prio=prio):
                    added += 1
        return added

    # ---- IMPROVE_CODE with genuine cross-model review ----
    def _improve_code(self, task) -> dict:
        fname = os.path.basename(task["spec"]) if task["spec"].endswith(".py") else None
        if not fname or fname in LOCKED or not os.path.exists(os.path.join(PKG, fname)):
            return {"status": "skipped", "why": "no valid/allowed target file"}
        src = open(os.path.join(PKG, fname)).read()
        cm = llm.ask_meta(CODER_SYS, f"Goal: {task.get('why') or task['spec']}\nFile earshot/{fname}:\n" + src[:10000],
                          want_json=True, max_tokens=1100, priority="low", task="code")
        patch = _json(cm.get("content"))
        coder = cm.get("provider")
        if not patch or not all(k in patch for k in ("find", "replace")):
            return {"status": "no_patch", "coder": coder}
        find, repl = patch["find"], patch["replace"]
        if find not in src or src.count(find) != 1 or repl == find or len(repl) > 1000:
            return {"status": "bad_anchor", "coder": coder}     # not a uniquely-applyable edit
        # REVIEWER — a DIFFERENT model than the coder
        rv = llm.ask_json(REVIEW_SYS,
                          f"File earshot/{fname}.\nFIND:\n{find}\n\nREPLACE:\n{repl}\n\nAuthor's reason: "
                          f"{patch.get('why', '')}", max_tokens=500, priority="normal", task="reason",
                          avoid={coder} if coder else None)
        if str((rv or {}).get("verdict", "kill")).lower() not in ("keep", "approve", "ok", "ship", "yes"):
            return {"status": "rejected_by_review", "coder": coder, "issues": (rv or {}).get("issues")}
        # JUDGE — fast veto
        jd = llm.ask_json(JUDGE_SYS, f"Patch to earshot/{fname}: {patch.get('why', '')}. "
                          f"Reviewer said: {json.dumps(rv)[:400]}. Ship it?",
                          max_tokens=160, priority="low", task="judge")
        if jd is not None and jd.get("ship") is False:
            return {"status": "vetoed_by_judge", "coder": coder, "why": jd.get("why")}
        # emit a cross-reviewed code card -> Telegram -> CI test-gate on approval
        if self.proposer is not None:
            self.proposer.add_code({"file": f"earshot/{fname}", "find": find, "replace": repl,
                                    "why": patch.get("why", task.get("why", "self-improvement"))},
                                   impact=self.code_impact)
        return {"status": "proposed_code", "file": fname, "coder": coder, "reviewer_ok": True}

    # ---- STRATEGIST: design a concrete testable strategy ----
    def _design_strategy(self, task, snapshot) -> dict:
        v = llm.ask_json(STRAT_SYS, f"Idea: {task['spec']}\nBrain:\n" + json.dumps(snapshot, default=str)[:2500],
                         max_tokens=600, priority="low", task="quant")
        if not v or not v.get("name"):
            return {"status": "no_strategy"}
        v["ts"] = iso(time.time())
        self.hv["strategies"].append(v)
        self.hv["strategies"] = self.hv["strategies"][-50:]      # bounded backlog for the sim/book to test
        return {"status": "designed_strategy", "name": str(v.get("name"))[:60]}

    # ---- SCOUT: ask Brady for a new source/tool via Telegram ----
    def _tool_or_source(self, task) -> dict:
        if self.approvals is None or not hasattr(self.approvals, "tool_request"):
            return {"status": "no_channel"}
        kind = "tool" if task["kind"] == "request_tool" else "data source"
        sent = self.approvals.tool_request(f"{kind}: {task['spec']}", task.get("why", ""))
        return {"status": "asked_brady" if sent else "not_sent", "kind": kind}
