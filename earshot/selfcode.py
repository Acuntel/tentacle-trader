"""Test-gated self-coding — the bot proposes a real change to its OWN source, PROVES it doesn't
break anything, and only then keeps it. Runs unattended (on a schedule) and commits passing changes
to the `self-evolve` branch for Brady to review and merge. He's out of the coding loop; he just
merges when he wants it live.

The gate a change must survive or it is auto-reverted:
  1. every module still compiles,
  2. `import earshot.main` still succeeds (no import-time break),
  3. a short simulation batch still runs without raising,
  4. FITNESS: replaying the last 400 REAL labeled setups through the predictor must not get WORSE
     (ledger Brier). "Doesn't crash" is not the same as "isn't dumber" — this turns self-coding from
     mutation-that-compiles into hill-climbing on measured performance. Skipped gracefully when no
     ledger is present (fresh CI checkout without the state backup).

Every kept/reverted patch also GRADES the model that wrote it (llm.feedback), so the router learns
which of the pool's models actually writes code that survives — the pool optimizes itself.

Three rails are NEVER self-editable: the hard risk caps (risk.py), the real-money gate (executor.py),
and the backtest Gauntlet (backtest.py). A proposed patch touching any of them is refused outright.

Run one improvement attempt:  python -m earshot.selfcode
Exit 0 + a modified working tree = a tested change ready to commit; exit 0 + clean tree = nothing kept.
"""
from __future__ import annotations

import json
import os
import py_compile
import subprocess
import sys
import time

from . import llm
from .models import iso

PKG = os.path.dirname(__file__)
EVO_DIR = "state/evolution"
APPLIED = os.path.join(EVO_DIR, "applied_patches.jsonl")
# the rails + the self-modification machinery itself — NEVER self-editable. Locking selfcode/apply_patch
# is what stops the system from removing its own guard; locking executor/risk/backtest is the money gate.
LOCKED = {"risk.py", "executor.py", "backtest.py", "selfcode.py", "apply_patch.py"}
# Any patch whose NEW code introduces these money-unlock tokens is refused outright — so full autonomy can
# never reach real money. This is what makes "it cannot invest real money yet" un-bypassable.
MONEY_GUARD = ("live_allowed", "RobinStocksExecutor", "get_executor", "order_buy", "order_sell",
               "account_number", "submit_order", "gauntlet_pass", "self.armed = True", "armed=True",
               "real_money", "place_order")


def _unsafe(code) -> bool:
    low = str(code)
    return any(tok in low for tok in MONEY_GUARD)

SYSTEM = ("You improve an autonomous trading bot's own Python source. Propose ONE small, safe, "
          "concrete change that fixes a bug or measurably improves it. You may NOT weaken the hard "
          "risk caps, the real-money gate, or the Gauntlet. "
          "HOT-PATH RULE: only patch code that actually EXECUTES — in `why`, name the caller that "
          "invokes what you changed. NEVER add or 'improve' a helper nothing calls; if you find dead "
          "code, propose DELETING it. A patch that merely survives tests but changes no runtime "
          "behavior is a failure, not a win. Reply ONLY as JSON: "
          '{"file": "earshot/<name>.py", "find": "<exact snippet present once>", '
          '"replace": "<the improved snippet>", "why": "<one sentence incl. the call site>"}. '
          "Keep `find` short and unique; `replace` must be valid Python.")

# Target priority (Brady's law for the self-coder):
#   1. FIX WHAT'S WRONG — a module the auditor flagged BROKEN jumps the whole queue.
#   2. IMPROVE ITSELF — most remaining effort goes to the RECURSIVE LOOP core, the modules that turn
#      signals into labels, labels into models, and models into decisions.
#   3. COVERAGE — the rest of the codebase keeps a minority share so plumbing bugs don't rot forever.
LOOP_CORE = ("learning.py", "learnedgate.py", "patterns.py", "brain.py", "core.py", "forge.py",
             "sim.py", "coach.py", "council.py", "proposer.py", "dossier.py", "memory.py",
             "histtrain.py", "warmstart.py", "enrich.py", "strategies.py", "evolve.py")
LOOP_SHARE = 0.75      # fraction of non-emergency attempts aimed at the loop core


def _broken_module() -> str | None:
    """The most recent module the 24/7 auditor flagged 'BROKEN — fix urgently' within the last 48h.
    Real breakage jumps the queue — fixing what's wrong beats improving what works."""
    cutoff = iso(time.time() - 48 * 3600)
    try:
        lines = open(os.path.join(EVO_DIR, "code_proposals.jsonl")).readlines()[-300:]
    except Exception:
        return None
    for ln in reversed(lines):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if str(o.get("status", "")).startswith("BROKEN") and str(o.get("ts", "")) >= cutoff:
            f = os.path.basename(str(o.get("file", "")))
            if f.endswith(".py") and f not in LOCKED and os.path.exists(os.path.join(PKG, f)):
                return f
    return None


def _pick_target(mods: list[str]) -> str:
    import random
    broken = _broken_module()
    if broken and broken in mods:
        return broken
    core = [m for m in mods if m in LOOP_CORE]
    rest = [m for m in mods if m not in LOOP_CORE]
    if core and (not rest or random.random() < LOOP_SHARE):
        return random.choice(core)
    return random.choice(rest or core)


def _mission() -> str:
    """A short, DATA-GROUNDED brief so the coder targets what actually matters (learning velocity,
    calibration, label throughput) instead of cosmetic refactors. Empty when no state is available."""
    try:
        from .persist import load_json
        k = load_json("state/knowledge.json", {})
        m = k.get("model", {})
        ex = k.get("explore", {})
        fld = k.get("base_rate", {}).get("ema")
        tr = ex.get("trades", 0)
        cl = k.get("change_lessons") or {}
        lessons = "; ".join((cl.get("standing") or [])[:1] + (cl.get("lessons") or [])[:3])
        return ("MISSION, in priority order: (1) FIX anything actually broken; (2) raise the bot's "
                "LEARNING VELOCITY — more labeled samples/day, better calibration, tighter feedback "
                "loops; (3) only then anything else. Current state: P(win) model "
                f"n={m.get('n', 0)} brier={m.get('brier', '?')} (lower=better), "
                f"explore trades={tr}"
                + (f" dir-hit={ex.get('dir_hits', 0)}/{tr}" if tr else "")
                + (f", field alpha-win base rate={fld}" if fld else "") +
                ". Avoid cosmetic refactors."
                + (f"\nMEASURED LESSONS from past self-changes (24h scoreboard): {lessons}" if lessons else "")
                + "\n\n")
    except Exception:
        return ""


def _modules() -> list[str]:
    # AUDIT FIX: exclude the LOCKED rails from target selection — the harness used to hand the coder
    # risk.py/executor.py/etc, then refuse its patch AND grade the model down for a target IT never chose.
    return sorted(f for f in os.listdir(PKG) if f.endswith(".py")
                  and f not in (LOCKED | {"__init__.py"}))


def _phantom_calls() -> list[str]:
    """AST lint: `self._private(...)` calls with no definition in the module. This is the exact
    hallucination class that took the live loop DOWN (2026-07-05): a patch invented
    `self._episode_from(...)` in sim.py — compile/import passed, and the CI sim-batch never reached
    the call because CI has no history cache, so the crash only fired in production. Underscore
    methods are class-internal by convention, so a missing one is a hard finding — and it's checkable
    statically, with zero data."""
    import ast
    bad = []
    for m in sorted(os.listdir(PKG)):
        if not m.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(PKG, m)).read())
        except SyntaxError:
            continue                                    # the compile gate reports this better
        defined = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        defined.add(t.attr)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
                    and node.func.attr.startswith("_") and not node.func.attr.startswith("__")
                    and node.func.attr not in defined):
                bad.append(f"{m}: self.{node.func.attr}() is not defined anywhere in the module")
    return bad


def _gate() -> tuple[bool, str]:
    """Prove the working tree is still healthy. Returns (ok, reason)."""
    for m in os.listdir(PKG):
        if m.endswith(".py"):
            try:
                py_compile.compile(os.path.join(PKG, m), doraise=True)
            except py_compile.PyCompileError as e:
                return False, f"compile fail {m}: {str(e).splitlines()[0][:120]}"
    phantoms = _phantom_calls()
    if phantoms:
        return False, "phantom method call (hallucinated helper): " + "; ".join(phantoms[:3])
    # fresh interpreter so edited modules are actually re-imported + a sim batch runs.
    # STATE-GROUNDED SMOKE: also exercise the data-dependent hot-path functions against the REAL
    # brain state (live volume, or the daily backup pulled into CI). Three outages shipped through
    # the old gate because their broken lines only execute with real data (sim._episode_from needed
    # a history cache; enrich's len(int) needed candidates; core.velocity's int('2026-07-06') needed
    # a growth history). Compile+import can't see those; running the functions on real state can.
    code = ("import yaml; cfg=yaml.safe_load(open('config.yaml'));"
            "import earshot.main;"
            "from earshot.learning import Learner; from earshot.sim import SimEngine;"
            "SimEngine(cfg, Learner(dict(cfg['source_weights']))).step();"
            "from earshot.persist import load_json;"
            "know=load_json('state/knowledge.json',{});"
            "from earshot.learnedgate import LearnedGate; from earshot.core import BrainCore;"
            "from earshot.patterns import PatternMemory, field_rate;"
            "lg=LearnedGate(know); pm=PatternMemory(know);"
            "core=BrainCore(know, dict(know.get('weights') or {}), cfg);"
            "core.velocity(lg); core.synthesize(pm); core.trajectory(); core.snapshot();"
            "field_rate(know);"
            "[(pm.multiplier(s), pm.is_loser(s), pm.is_decayed(s)) for s in list(know.get('patterns') or {})[:25]];"
            "print('GATE_OK')")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, f"gate run error: {e}"
    if r.returncode != 0 or "GATE_OK" not in r.stdout:
        return False, "import/sim gate failed: " + (r.stderr.strip().splitlines() or [""])[-1][:160]
    return True, "compiles + imports + sim runs"


FITNESS_TOL = 0.005      # a patch may not worsen the ledger Brier by more than this
_FIT_CODE = r"""
import json
rows = []
try:
    with open('state/resolved_obs.jsonl') as f:
        for line in f:
            try:
                o = json.loads(line)
                if o.get('feat') and o.get('win') is not None:
                    rows.append(o)
            except Exception:
                pass
except Exception:
    pass
rows = rows[-400:]
if len(rows) < 50:
    print('FIT ' + json.dumps({'n': 0}))
else:
    from earshot.persist import load_json
    from earshot.learnedgate import LearnedGate
    g = LearnedGate(load_json('state/knowledge.json', {}))
    b = 0.0
    for r in rows:
        p = g.predict(r['feat'])
        b += (p - (1.0 if r['win'] else 0.0)) ** 2
    print('FIT ' + json.dumps({'n': len(rows), 'brier': round(b / len(rows), 5)}))
"""


def _fitness() -> dict | None:
    """Deterministic FITNESS: replay the last 400 real labeled setups (resolved_obs.jsonl) through the
    current predictor in a fresh interpreter and measure Brier. Same data + same learned weights ->
    same number, so a before/after comparison isolates exactly what the patch changed. Returns None
    when no ledger is available — then only the break-gate applies."""
    try:
        r = subprocess.run([sys.executable, "-c", _FIT_CODE], capture_output=True, text=True, timeout=60)
        for line in (r.stdout or "").splitlines():
            if line.startswith("FIT "):
                d = json.loads(line[4:])
                return d if d.get("n") else None
    except Exception:
        pass
    return None


def _fitness_check(base: dict | None) -> tuple[bool, str]:
    """Compare post-patch fitness to the pre-patch baseline. Pass when no baseline exists."""
    if not base:
        return True, "no ledger — fitness skipped"
    after = _fitness()
    if not after:
        return True, "fitness unavailable post-patch — skipped"
    if after["brier"] > base["brier"] + FITNESS_TOL:
        return False, (f"FITNESS REGRESSION: ledger Brier {base['brier']} -> {after['brier']} "
                       "(the patch made the brain worse at predicting real outcomes)")
    return True, f"fitness OK (Brier {base['brier']} -> {after['brier']})"


def _propose() -> tuple[dict | None, str | None]:
    """Ask the pool's best measured coder for ONE concrete patch. Returns (proposal, provider) so the
    gate's verdict can GRADE the model that wrote it (the router learns who actually codes well)."""
    # weighted target: the recursive-loop core gets most attempts (blind clock-rotation guaranteed
    # cosmetic tweaks to peripheral modules — the loop is what we're here to improve)
    mod = _pick_target(_modules())
    src = open(os.path.join(PKG, mod)).read()
    r = llm.ask_meta(SYSTEM, _mission() + f"Improve earshot/{mod}. Source:\n" + src[:11000],
                     want_json=True, max_tokens=1200, priority="low", task="code")
    txt, name = r.get("content"), r.get("provider")
    if not txt:
        return None, None
    try:
        v = json.loads(txt)
    except Exception:
        try:
            s, e = txt.index("{"), txt.rindex("}")
            v = json.loads(txt[s:e + 1])
        except Exception:
            llm.feedback(name, "code", False)             # answered, but unusable output
            return None, name
    if not v or not all(k in v for k in ("file", "find", "replace")):
        llm.feedback(name, "code", False)
        return None, name
    return v, name


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def attempt() -> dict:
    """One self-improvement attempt: propose -> apply -> gate + fitness -> keep or revert.
    The verdict GRADES the model that wrote the patch, so the router learns who codes well for us."""
    if not llm.have_free_brain():
        return {"status": "skipped", "why": "no free LLM (set GROQ_API_KEY)"}
    p, coder = _propose()
    if not p:
        return {"status": "skipped", "why": "no concrete proposal"}

    fname = os.path.basename(str(p["file"]))
    path = os.path.join(PKG, fname)
    if fname in LOCKED:
        llm.feedback(coder, "code", False)                  # proposing a locked rail = a bad proposal
        return {"status": "refused", "why": f"{fname} is a locked rail", "file": p["file"]}
    if not os.path.exists(path):
        llm.feedback(coder, "code", False)
        return {"status": "refused", "why": "unknown file", "file": p["file"]}
    src = open(path).read()
    if p["find"] not in src or src.count(p["find"]) != 1 or p["replace"] == p["find"]:
        llm.feedback(coder, "code", False)                  # hallucinated snippet = a bad proposal
        return {"status": "refused", "why": "find not uniquely present (no safe edit point)",
                "file": p["file"]}
    if _unsafe(p["replace"]):
        llm.feedback(coder, "code", False)
        return {"status": "refused", "why": "patch touches the money/safety gate", "file": p["file"]}

    base_fit = _fitness()                                   # measure BEFORE the patch (deterministic)
    new = src.replace(p["find"], p["replace"])
    open(path, "w").write(new)                              # apply
    ok, reason = _gate()                                    # prove it doesn't break
    if ok:
        ok, fit_reason = _fitness_check(base_fit)           # prove it isn't DUMBER
        reason = f"{reason}; {fit_reason}"
    if ok:
        llm.feedback(coder, "code", True)                   # this model writes code that survives
        rec = {"ts": iso(time.time()), "status": "TESTED+APPLIED (commit to self-evolve)",
               "file": p["file"], "why": p.get("why"), "gate": reason, "coder": coder}
        _append(APPLIED, rec)
        return {"status": "applied", **rec}
    open(path, "w").write(src)                              # auto-revert — the gate caught a break
    llm.feedback(coder, "code", False)
    rec = {"ts": iso(time.time()), "status": "REVERTED (failed the gate)",
           "file": p["file"], "why": p.get("why"), "gate": reason, "coder": coder}
    _append(APPLIED, rec)
    return {"status": "reverted", **rec}


def _restore(originals: dict):
    for path, src in originals.items():
        open(path, "w").write(src)


def apply_external(patches: list) -> dict:
    """Apply a list of APPROVED {file, find, replace} patches, then prove the whole tree still passes
    the gate (compile + import + sim). Allows earshot/*.py and config.yaml; refuses the 3 locked rails.
    On any gate failure, reverts EVERY patch. Used by the apply-approved Action after a Telegram tap —
    the live trader never writes its own code; an isolated runner applies exactly what Brady approved."""
    repo = os.path.dirname(PKG)
    base_fit = _fitness()                                  # baseline on the untouched tree
    originals: dict = {}
    applied: list = []
    for p in patches or []:
        rel = str(p.get("file", "")).strip().lstrip("./")
        fname = os.path.basename(rel)
        if rel == "config.yaml":
            path = os.path.join(repo, "config.yaml")
        elif fname in LOCKED:
            _restore(originals)
            return {"status": "refused", "why": f"{fname} is a locked rail"}
        elif rel.startswith("earshot/") and fname.endswith(".py"):
            path = os.path.join(PKG, fname)
        else:
            _restore(originals)
            return {"status": "refused", "why": f"path not allowed: {rel}"}
        if not os.path.exists(path):
            _restore(originals)
            return {"status": "refused", "why": f"unknown file {rel}"}
        src = open(path).read()
        find, repl = p.get("find", ""), p.get("replace", "")
        if not find or find not in src or src.count(find) != 1 or repl == find:
            _restore(originals)
            return {"status": "refused", "why": f"find not uniquely present in {rel}"}
        if _unsafe(repl):
            _restore(originals)
            return {"status": "refused", "why": f"patch touches the money/safety gate ({rel})"}
        originals.setdefault(path, src)
        open(path, "w").write(src.replace(find, repl))
        applied.append(rel)
    if not applied:
        return {"status": "skipped", "why": "no patches"}
    ok, reason = _gate()
    if ok:
        ok, fit_reason = _fitness_check(base_fit)          # approved code must not make the brain dumber
        reason = f"{reason}; {fit_reason}"
    if ok:
        _append(APPLIED, {"ts": iso(time.time()), "status": "APPROVED+TESTED (apply-approved)",
                          "files": applied, "gate": reason})
        return {"status": "applied", "files": applied, "gate": reason}
    _restore(originals)
    return {"status": "reverted", "files": applied, "gate": reason}


def main():
    r = attempt()
    print(json.dumps(r, indent=2))
    # exit 0 always; a modified tree (status == applied) is the signal to commit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
