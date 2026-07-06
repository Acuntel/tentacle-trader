"""Recall — the connective tissue. Nothing the system has ever produced is wasted. Past code-improvement
ideas, designed strategies, proposed data sources, code that FAILED the gate, and hard-won loss lessons are
all loaded back INTO the live loop so they keep having utility:

  - the Hive COORDINATOR revives past ideas (rotating cursor, so EVERYTHING eventually resurfaces),
  - it learns from past lessons / mistakes and retries worthwhile reverted code,
  - and it never re-proposes ideas Brady already denied.

Old work compounds the system instead of rotting in a journal. Pure read-only over the brain + journals.
"""
from __future__ import annotations

import json
import os

EVO = "state/evolution"
PROPOSALS = os.path.join(EVO, "code_proposals.jsonl")
APPLIED = os.path.join(EVO, "applied_patches.jsonl")
DUNGEON = "state/dungeon/post_mortems.jsonl"


def _tail_jsonl(path, n):
    out = []
    try:
        with open(path) as f:
            for line in f.readlines()[-n:]:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def backlog(know, k: int = 6):
    """A ROTATING sample of past, still-relevant ideas to revive: journaled code proposals + Hive-designed
    strategies + scout-proposed sources. The cursor advances each call so nothing stays buried forever."""
    items = []
    denied = set((know.get("approvals", {}).get("denied") or {}).keys())
    for p in _tail_jsonl(PROPOSALS, 80):
        what = p.get("change") or p.get("why")
        if what:
            items.append({"kind": "code_idea", "file": p.get("file"), "what": what})
    for s in (know.get("hive", {}).get("strategies") or [])[-25:]:
        if s.get("name"):
            items.append({"kind": "strategy", "what": s.get("name"), "why": s.get("rationale")})
    for rev in reverted(10):
        if rev.get("file"):
            items.append({"kind": "reverted", "file": rev["file"], "what": rev.get("why")})
    for src in (know.get("proposed_sources") or [])[-25:]:
        items.append({"kind": "source", "what": src})
    # dedupe + drop anything already denied
    seen, uniq = set(), []
    for it in items:
        key = f"{it['kind']}:{it.get('what')}"
        if key in seen or key in denied:
            continue
        seen.add(key)
        uniq.append(it)
    if not uniq:
        return []
    rc = know.setdefault("recall", {})
    cur = int(rc.get("cursor", 0))
    n = min(k, len(uniq))
    pick = [uniq[(cur + i) % len(uniq)] for i in range(n)]
    rc["cursor"] = (cur + n) % len(uniq)
    return pick


def lessons(know, k: int = 6):
    out = list((know.get("lessons") or [])[-k:])
    for pm in _tail_jsonl(DUNGEON, k):
        rc = pm.get("root_cause")
        if rc:
            out.append(rc)
    return out[-k:]


def reverted(k: int = 3):
    """Code attempts that FAILED the gate — lost code worth retrying properly."""
    return [{"file": p.get("file"), "why": p.get("why")} for p in _tail_jsonl(APPLIED, 50)
            if "REVERT" in str(p.get("status", "")).upper()][-k:]
