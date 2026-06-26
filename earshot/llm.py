"""The reasoning brain — free first, paid optional. Two near-identically-named providers, both
OpenAI-compatible:

  • GROQ  (gsk_… key)  — FREE tier, fast Llama. This is the $0 default. Set GROQ_API_KEY.
  • xAI / GROK (xai-… key) — Elon's Grok. PAID per token (this is what charges you). Set XAI_API_KEY.

Whichever key is present is used (Groq wins if both are set, because it's free). The rest of the bot
COMBINES this with the rule heuristic — it's never a sole dependency, and the heuristic keeps it alive
if no key is set or a call blips. Never raises into the loop.
"""
from __future__ import annotations

import json
import os
import time

try:
    import requests
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
XAI_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_XAI_MODEL = "grok-2-latest"

# ---- Daily FREE-tier token budget (Groq is ~100k tokens/day). Without this, the high-volume per-cycle
# analyst burns the whole day's budget early, and the coach + proposer (the loop-critical + user-facing
# callers) get 429'd for the rest of the day. We track usage, persist it across redeploys, and gate each
# call by PRIORITY so low-priority callers back off early and leave headroom for the important ones. ----
_BUDGET_PATH = "state/groq_budget.json"
DAILY_TOKENS = int(os.getenv("GROQ_DAILY_TOKENS", "95000"))   # soft cap under the 100k hard limit
RESERVE = {"low": 0.50, "normal": 0.80, "high": 0.95}         # fraction of the day a priority may use


def _budget_today() -> dict:
    from .persist import load_json
    b = load_json(_BUDGET_PATH, {"day": "", "used": 0})
    day = time.strftime("%Y-%m-%d")
    if b.get("day") != day:
        b = {"day": day, "used": 0}
    return b


def tokens_used_today() -> int:
    return int(_budget_today().get("used", 0))


def budget_ok(priority: str = "normal", est: int = 1200) -> bool:
    cap = DAILY_TOKENS * RESERVE.get(priority, 0.80)
    return tokens_used_today() + est <= cap


def _spend(tokens: int):
    from .persist import save_json
    b = _budget_today()
    b["used"] = int(b.get("used", 0)) + max(0, int(tokens))
    save_json(_BUDGET_PATH, b)


def _provider():
    """(name, url, key, model, is_free) for the active provider, or Nones. Groq (free) wins."""
    if os.getenv("GROQ_API_KEY"):
        return ("groq", GROQ_URL, os.getenv("GROQ_API_KEY"),
                os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL), True)
    if os.getenv("XAI_API_KEY"):
        return ("xai", XAI_URL, os.getenv("XAI_API_KEY"),
                os.getenv("XAI_MODEL", DEFAULT_XAI_MODEL), False)
    return (None, None, None, None, False)


def have_brain() -> bool:
    """An LLM (free Groq or paid xAI) is reachable."""
    return _HAVE and _provider()[0] is not None


def have_free_brain() -> bool:
    """Specifically a FREE brain (Groq) is reachable."""
    return _HAVE and _provider()[4]


def provider() -> str:
    name, _, _, model, free = _provider()
    return f"{name}:{model} ({'free' if free else 'PAID'})" if name else "heuristic"


def ask(system: str, user: str, want_json: bool = False, max_tokens: int = 1024,
        temperature: float = 0.3, timeout: float = 30.0, priority: str = "normal") -> str | None:
    name, url, key, model, is_free = _provider()
    if not (_HAVE and key):
        return None
    # on the free tier, leave the daily budget for higher-priority callers (proposer > coach > analyst)
    if is_free and not budget_ok(priority, est=max_tokens + len(user) // 4):
        return None
    body = {"model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temperature}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(url, json=body, timeout=timeout, headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            return None
        data = r.json()
        if is_free:
            _spend((data.get("usage") or {}).get("total_tokens", max_tokens))
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def ask_json(system: str, user: str, **kw) -> dict | None:
    txt = ask(system, user, want_json=True, **kw)
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
