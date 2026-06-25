"""Test-gated self-coding — the bot proposes a real change to its OWN source, PROVES it doesn't
break anything, and only then keeps it. Runs unattended (on a schedule) and commits passing changes
to the `self-evolve` branch for Brady to review and merge. He's out of the coding loop; he just
merges when he wants it live.

The gate a change must survive or it is auto-reverted:
  1. every module still compiles,
  2. `import earshot.main` still succeeds (no import-time break),
  3. a short simulation batch still runs without raising.

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
LOCKED = {"risk.py", "executor.py", "backtest.py"}        # the rails — refuse to self-edit these

SYSTEM = ("You improve an autonomous trading bot's own Python source. Propose ONE small, safe, "
          "concrete change that fixes a bug or measurably improves it. You may NOT weaken the hard "
          "risk caps, the real-money gate, or the Gauntlet. Reply ONLY as JSON: "
          '{"file": "earshot/<name>.py", "find": "<exact snippet present once>", '
          '"replace": "<the improved snippet>", "why": "<one sentence>"}. '
          "Keep `find` short and unique; `replace` must be valid Python.")


def _modules() -> list[str]:
    return sorted(f for f in os.listdir(PKG) if f.endswith(".py")
                  and f not in {"__init__.py", "selfcode.py"})


def _gate() -> tuple[bool, str]:
    """Prove the working tree is still healthy. Returns (ok, reason)."""
    for m in os.listdir(PKG):
        if m.endswith(".py"):
            try:
                py_compile.compile(os.path.join(PKG, m), doraise=True)
            except py_compile.PyCompileError as e:
                return False, f"compile fail {m}: {str(e).splitlines()[0][:120]}"
    # fresh interpreter so edited modules are actually re-imported + a sim batch runs
    code = ("import yaml; cfg=yaml.safe_load(open('config.yaml'));"
            "import earshot.main;"
            "from earshot.learning import Learner; from earshot.sim import SimEngine;"
            "SimEngine(cfg, Learner(dict(cfg['source_weights']))).step();"
            "print('GATE_OK')")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, f"gate run error: {e}"
    if r.returncode != 0 or "GATE_OK" not in r.stdout:
        return False, "import/sim gate failed: " + (r.stderr.strip().splitlines() or [""])[-1][:160]
    return True, "compiles + imports + sim runs"


def _propose() -> dict | None:
    mods = _modules()
    # rotate target so the whole codebase evolves over time
    idx = int(time.time() // 3600) % len(mods)
    mod = mods[idx]
    src = open(os.path.join(PKG, mod)).read()
    v = llm.ask_json(SYSTEM, f"Improve earshot/{mod}. Source:\n" + src[:11000], max_tokens=1200)
    if not v or not all(k in v for k in ("file", "find", "replace")):
        return None
    return v


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def attempt() -> dict:
    """One self-improvement attempt: propose -> apply -> gate -> keep or revert."""
    if not llm.have_free_brain():
        return {"status": "skipped", "why": "no free LLM (set GROQ_API_KEY)"}
    p = _propose()
    if not p:
        return {"status": "skipped", "why": "no concrete proposal"}

    fname = os.path.basename(str(p["file"]))
    path = os.path.join(PKG, fname)
    if fname in LOCKED:
        return {"status": "refused", "why": f"{fname} is a locked rail", "file": p["file"]}
    if not os.path.exists(path):
        return {"status": "refused", "why": "unknown file", "file": p["file"]}
    src = open(path).read()
    if p["find"] not in src or src.count(p["find"]) != 1 or p["replace"] == p["find"]:
        return {"status": "refused", "why": "find not uniquely present (no safe edit point)",
                "file": p["file"]}

    new = src.replace(p["find"], p["replace"])
    open(path, "w").write(new)                              # apply
    ok, reason = _gate()                                    # prove it
    if ok:
        rec = {"ts": iso(time.time()), "status": "TESTED+APPLIED (commit to self-evolve)",
               "file": p["file"], "why": p.get("why"), "gate": reason}
        _append(APPLIED, rec)
        return {"status": "applied", **rec}
    open(path, "w").write(src)                              # auto-revert — the gate caught a break
    rec = {"ts": iso(time.time()), "status": "REVERTED (failed the gate)",
           "file": p["file"], "why": p.get("why"), "gate": reason}
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
        originals.setdefault(path, src)
        open(path, "w").write(src.replace(find, repl))
        applied.append(rel)
    if not applied:
        return {"status": "skipped", "why": "no patches"}
    ok, reason = _gate()
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
