"""Off-volume backup of the learned brain to a PRIVATE GitHub repo, so a Railway volume loss can never
erase the bot's learning. Uses GITHUB_DISPATCH_TOKEN (already set for the approval loop). Commits the
small, irreplaceable state files (NOT the ~29MB regenerable history) once/day via the GitHub Contents
API — each file is well under the API's comfortable size, so no git-data plumbing needed.
"""
from __future__ import annotations

import base64
import os
import time

try:
    import requests
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

API = "https://api.github.com"
# the irreplaceable brain (history/ is excluded — it's re-downloadable yfinance bars)
CRITICAL = ["knowledge.json", "outcomes.jsonl", "sim.json", "ladder.json", "open_positions.json",
            "observations.json", "resolved_obs.jsonl", "self_review.jsonl"]
MAX_FILE = 5_000_000        # skip anything large (the contents API is for small files)


def _token() -> str:
    return os.getenv("GITHUB_DISPATCH_TOKEN") or os.getenv("GH_TOKEN") or ""


def _put(repo: str, token: str, path: str, data: bytes, msg: str) -> bool:
    url = f"{API}/repos/{repo}/contents/{path}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    sha = None
    try:
        g = requests.get(url, headers=h, timeout=20)
        if g.status_code == 200:
            sha = g.json().get("sha")               # overwrite the existing file
    except Exception:
        pass
    body = {"message": msg, "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=h, json=body, timeout=40)
        return r.status_code in (200, 201)
    except Exception:
        return False


def push_state(repo: str | None, state_dir: str = "state", files=None) -> dict:
    """Commit the critical state files to `repo` under latest/. Returns a status summary. Safe no-op
    without a token/repo/requests (so it never breaks the loop)."""
    if not (_HAVE and _token() and repo):
        return {"status": "skipped", "why": "no token/repo/requests"}
    token = _token()
    day = time.strftime("%Y-%m-%d")
    ok, fail = [], []
    for name in (files or CRITICAL):
        p = os.path.join(state_dir, name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "rb") as f:
                data = f.read()
        except Exception:
            continue
        if len(data) > MAX_FILE:
            continue
        (ok if _put(repo, token, f"latest/{name}", data, f"state backup {day}: {name}") else fail).append(name)
    return {"status": "ok" if (ok and not fail) else ("partial" if ok else "failed"),
            "backed_up": ok, "failed": fail, "day": day}
