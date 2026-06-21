"""24/7 self-code audit — the bot reads and critiques its OWN source on a loop, forever.

Every pass it: (1) compile-checks every module (a real break = a high-severity self-report),
(2) scans one rotating module for code smells, and (3) has the free LLM review that module's source
for bugs + improvements. Findings + proposed fixes go to the evolution journal for Brady to review
and merge. It NEVER edits its own code here (propose-only) — and it can never touch the locked rails
(hard caps, real-money gate, the Gauntlet). Over time it walks the whole codebase again and again,
always hunting for ways to become a better trader.
"""
from __future__ import annotations

import json
import os
import py_compile
import time

from . import llm
from .models import iso

PKG = os.path.dirname(__file__)
EVO_DIR = "state/evolution"
LOG_MD = os.path.join(EVO_DIR, "EVOLUTION_LOG.md")
PROPOSALS = os.path.join(EVO_DIR, "code_proposals.jsonl")
AUDIT_STATE = "state/audit.json"

SYSTEM = ("You are a meticulous code reviewer for an autonomous trading bot. Review ONE Python "
          "module for real bugs, risks, and concrete improvements that would make it a safer or "
          "better trader. Never propose weakening the hard risk caps, the real-money gate, or the "
          "backtest Gauntlet. Reply ONLY as a JSON object.")


def _modules() -> list[str]:
    return sorted(f for f in os.listdir(PKG) if f.endswith(".py") and f != "__init__.py")


def _smells(src: str) -> list[str]:
    out = []
    if "except:" in src or "except Exception: pass" in src:
        out.append("broad/silent except — may swallow real errors")
    n = src.count("\n")
    if n > 300:
        out.append(f"{n} lines — over the 300-line limit, consider splitting")
    for marker in ("TODO", "FIXME", "XXX", "HACK"):
        if marker in src:
            out.append(f"contains {marker}")
    return out


def _append(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Auditor:
    def __init__(self, cfg: dict):
        c = cfg.get("audit", {})
        self.on = bool(c.get("enabled", True))
        self.every = int(c.get("every_minutes", 60)) * 60
        self.last = 0.0
        try:
            self.idx = json.load(open(AUDIT_STATE)).get("idx", 0)
        except Exception:
            self.idx = 0

    def due(self) -> bool:
        return self.on and (time.time() - self.last) >= self.every

    def run(self) -> dict:
        self.last = time.time()
        mods = _modules()
        if not mods:
            return {"module": None, "broken": [], "findings": 0}

        # (1) HEALTH — compile every module; a failure is a real, self-detected break.
        broken = []
        for m in mods:
            try:
                py_compile.compile(os.path.join(PKG, m), doraise=True)
            except py_compile.PyCompileError as e:
                broken.append({"file": f"earshot/{m}", "error": str(e).splitlines()[0][:200]})

        # (2) + (3) — review ONE rotating module (whole codebase gets swept over time).
        mod = mods[self.idx % len(mods)]
        self.idx = (self.idx + 1) % len(mods)
        json.dump({"idx": self.idx}, open(AUDIT_STATE, "w"))
        src = open(os.path.join(PKG, mod)).read()
        smells = _smells(src)

        findings = []
        v = llm.ask_json(SYSTEM,
                         f"Review earshot/{mod}. Reply as JSON with keys: bugs (array of "
                         "{issue, severity}), improvements (array of strings), proposed_fix (string, "
                         "one concrete change or empty). Source:\n" + src[:11000],
                         max_tokens=1100)
        if v:
            for b in (v.get("bugs") or [])[:6]:
                if isinstance(b, dict):
                    findings.append({"kind": "bug", "issue": b.get("issue"),
                                     "severity": b.get("severity")})
            for imp in (v.get("improvements") or [])[:6]:
                findings.append({"kind": "improvement", "issue": str(imp)})
            if v.get("proposed_fix"):
                findings.append({"kind": "proposed_fix", "issue": str(v["proposed_fix"])})

        # journal everything — propose-only, for human review
        for s in smells:
            findings.append({"kind": "smell", "issue": s})
        for b in broken:
            _append(PROPOSALS, {"ts": iso(time.time()), "status": "BROKEN — fix urgently",
                                "file": b["file"], "change": b["error"], "why": "module fails to compile"})
        for fnd in findings:
            _append(PROPOSALS, {"ts": iso(time.time()), "status": "AUDIT — review & merge",
                                "file": f"earshot/{mod}", "change": fnd["issue"],
                                "why": f"{fnd['kind']}{(' / ' + str(fnd.get('severity'))) if fnd.get('severity') else ''}"})
        self._log(mod, broken, findings, by="groq+static" if v else "static")
        return {"module": mod, "broken": broken, "findings": len(findings),
                "by": "groq+static" if v else "static"}

    def _log(self, mod, broken, findings, by):
        os.makedirs(EVO_DIR, exist_ok=True)
        block = [f"\n## {iso(time.time())}  ·  self-code audit ({by})",
                 f"- module reviewed: `earshot/{mod}`",
                 f"- compile health: {'ALL OK' if not broken else '⚠ BROKEN: ' + ', '.join(b['file'] for b in broken)}",
                 f"- findings (propose-only): {len(findings)}"]
        for fnd in findings[:8]:
            block.append(f"    - [{fnd['kind']}] {fnd['issue']}")
        with open(LOG_MD, "a") as f:
            f.write("\n".join(block) + "\n")
