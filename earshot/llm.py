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

try:
    import requests
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
XAI_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_XAI_MODEL = "grok-2-latest"


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


def ask(system: str, user: str, want_json: bool = False,
        max_tokens: int = 1024, temperature: float = 0.3, timeout: float = 30.0) -> str | None:
    name, url, key, model, _ = _provider()
    if not (_HAVE and key):
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
        return r.json()["choices"][0]["message"]["content"]
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
