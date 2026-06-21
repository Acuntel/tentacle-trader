"""Free reasoning brain. Groq first (free, OpenAI-compatible, Llama 3.3 70B), then nothing —
the CALLER falls back to its own heuristic. This is the "combination of Groq + heuristic" the
bot asked for: an LLM when one is reachable for free, rules-based reasoning when it isn't.

Zero cost by design: Groq's free tier needs only GROQ_API_KEY. Anthropic stays optional and OFF
unless ANTHROPIC_API_KEY is set AND analyst.provider == 'anthropic' (it is NOT free).

Never raises into the loop — every failure returns None so the caller degrades to heuristics.
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
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"   # free, fast, smart enough to coach the bot


def have_free_brain() -> bool:
    """A free LLM is reachable iff requests is installed and a Groq key is present."""
    return _HAVE and bool(os.getenv("GROQ_API_KEY"))


def provider() -> str:
    if have_free_brain():
        return f"groq:{os.getenv('GROQ_MODEL', DEFAULT_GROQ_MODEL)}"
    return "heuristic"


def ask(system: str, user: str, want_json: bool = False,
        max_tokens: int = 1024, temperature: float = 0.3, timeout: float = 30.0) -> str | None:
    """Ask the free brain one question. Returns the text reply (or None on any failure).
    With want_json=True, asks Groq for a JSON object and returns the raw JSON string."""
    key = os.getenv("GROQ_API_KEY")
    if not (_HAVE and key):
        return None
    body = {
        "model": os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if want_json:
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(GROQ_URL, json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            return None
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def ask_json(system: str, user: str, **kw) -> dict | None:
    """Ask for a JSON object and parse it. None if the brain is offline or the reply isn't JSON."""
    txt = ask(system, user, want_json=True, **kw)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        # models sometimes wrap JSON in prose/fences — salvage the first {...} block
        try:
            s, e = txt.index("{"), txt.rindex("}")
            return json.loads(txt[s:e + 1])
        except Exception:
            return None
