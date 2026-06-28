"""The reasoning layer — a POOL of free LLMs working in unison, each routed to what it's best at.

Every provider here is OpenAI-chat-compatible, so one `_call` drives them all. For each task we try a
PREFERRED chain of providers (best-for-the-job first); if one is missing a key, rate-limited (429), or
over its daily budget, we fail over to the next. So losing any single provider (e.g. Groq hits its
100k-tokens/day free limit) doesn't stop the bot — the others cover. Per-provider daily token budgets
are tracked + persisted, and a PRIORITY reserve keeps the loop-critical callers (coach/proposer) from
being starved by the high-volume analyst.

All FREE (activate by setting the env key); paid ones (deepseek-direct, moonshot, xai) stay off unless set:
  GROQ_API_KEY        Groq — fast Llama, free 100k tok/day                  (US)
  GEMINI_API_KEY      Google Gemini Flash — smart, big free budget          (US)
  CEREBRAS_API_KEY    Cerebras — fastest inference, free                    (US)
  SAMBANOVA_API_KEY   SambaNova — fast Llama-3.3-70B, free tier             (US)
  NVIDIA_API_KEY      Nvidia NIM — many models incl DeepSeek-R1, free tier  (US)
  OPENROUTER_API_KEY  OpenRouter — ONE key, free routes to DeepSeek/Qwen/GLM/Kimi/Llama (the easy "all the Chinese models" unlock)
  GLM_API_KEY         Zhipu GLM-4-Flash — free Chinese model                (CN)
  DASHSCOPE_API_KEY   Alibaba Qwen — free quota                             (CN)
  MOONSHOT_API_KEY    Moonshot / Kimi K2 — Chinese, trial credits then paid (CN)
  DEEPSEEK_API_KEY    DeepSeek direct (paid, cheap) — off unless set        (CN)
  XAI_API_KEY         xAI Grok (paid) — off unless set                      (US)
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

# name -> endpoint config. `daily` = soft daily TOKEN budget (the 429-cooldown is the real guard).
PROVIDERS = {
    "groq":       {"url": "https://api.groq.com/openai/v1/chat/completions",
                   "key": "GROQ_API_KEY", "model_env": "GROQ_MODEL",
                   "model": "llama-3.3-70b-versatile", "free": True, "daily": 95_000},
    "gemini":     {"url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                   "key": "GEMINI_API_KEY", "model_env": "GEMINI_MODEL",
                   "model": "gemini-2.0-flash", "free": True, "daily": 3_000_000},
    "cerebras":   {"url": "https://api.cerebras.ai/v1/chat/completions",
                   "key": "CEREBRAS_API_KEY", "model_env": "CEREBRAS_MODEL",
                   "model": "llama-3.3-70b", "free": True, "daily": 1_000_000},
    "sambanova":  {"url": "https://api.sambanova.ai/v1/chat/completions",
                   "key": "SAMBANOVA_API_KEY", "model_env": "SAMBANOVA_MODEL",
                   "model": "Meta-Llama-3.3-70B-Instruct", "free": True, "daily": 1_000_000},
    "nvidia":     {"url": "https://integrate.api.nvidia.com/v1/chat/completions",
                   "key": "NVIDIA_API_KEY", "model_env": "NVIDIA_MODEL",
                   "model": "meta/llama-3.3-70b-instruct", "free": True, "daily": 1_000_000},
    "moonshot":   {"url": "https://api.moonshot.ai/v1/chat/completions",
                   "key": "MOONSHOT_API_KEY", "model_env": "MOONSHOT_MODEL",
                   "model": "kimi-k2-0711-preview", "free": False, "daily": 10 ** 9},
    "glm":        {"url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                   "key": "GLM_API_KEY", "model_env": "GLM_MODEL",
                   "model": "glm-4-flash", "free": True, "daily": 2_000_000},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions",
                   "key": "OPENROUTER_API_KEY", "model_env": "OPENROUTER_MODEL",
                   "model": "meta-llama/llama-3.3-70b-instruct:free", "free": True, "daily": 2_000_000},
    "qwen":       {"url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
                   "key": "DASHSCOPE_API_KEY", "model_env": "QWEN_MODEL",
                   "model": "qwen-plus", "free": True, "daily": 1_000_000},
    "deepseek":   {"url": "https://api.deepseek.com/chat/completions",
                   "key": "DEEPSEEK_API_KEY", "model_env": "DEEPSEEK_MODEL",
                   "model": "deepseek-chat", "free": False, "daily": 10 ** 9},
    "xai":        {"url": "https://api.x.ai/v1/chat/completions",
                   "key": "XAI_API_KEY", "model_env": "XAI_MODEL",
                   "model": "grok-2-latest", "free": False, "daily": 10 ** 9},
}

# task -> preferred provider order (best-for-the-job first). Free DeepSeek/Qwen reach code/reason via
# OpenRouter even without their own keys. Missing-key / rate-limited / over-budget providers are skipped.
ROUTING = {
    "judge":   ["groq", "cerebras", "sambanova", "gemini", "nvidia", "glm", "qwen", "openrouter"],   # fast, high-volume
    "reason":  ["gemini", "deepseek", "nvidia", "groq", "qwen", "glm", "cerebras", "sambanova", "openrouter"],  # deep reasoning
    "code":    ["deepseek", "moonshot", "glm", "qwen", "nvidia", "openrouter", "gemini", "groq"],     # code-strong first (DeepSeek/Kimi/GLM/Qwen)
    "propose": ["gemini", "groq", "deepseek", "nvidia", "qwen", "glm", "sambanova", "openrouter"],    # quality + evidence
    "default": ["groq", "gemini", "cerebras", "sambanova", "glm", "qwen", "nvidia", "openrouter"],
}
RESERVE = {"low": 0.50, "normal": 0.80, "high": 0.95}     # per-provider daily fraction a priority may use
_BUDGET_PATH = "state/llm_budget.json"
_COOLDOWN: dict[str, float] = {}                          # name -> epoch until which to skip (after a 429)


def _has_key(name: str) -> bool:
    return bool(os.getenv(PROVIDERS[name]["key"]))


def have_brain() -> bool:
    return _HAVE and any(_has_key(n) for n in PROVIDERS)


def have_free_brain() -> bool:
    return _HAVE and any(_has_key(n) for n, p in PROVIDERS.items() if p["free"])


def active() -> list[str]:
    return [n for n in PROVIDERS if _has_key(n)]


def provider() -> str:
    a = active()
    return ("pool: " + ", ".join(a)) if a else "heuristic"


# ---- per-provider daily token budget (persisted; resets daily) ----
def _budget_today() -> dict:
    from .persist import load_json
    b = load_json(_BUDGET_PATH, {"day": "", "used": {}})
    if b.get("day") != time.strftime("%Y-%m-%d"):
        b = {"day": time.strftime("%Y-%m-%d"), "used": {}}
    return b


def _budget_ok(name: str, priority: str, est: int) -> bool:
    b = _budget_today()
    cap = PROVIDERS[name]["daily"] * RESERVE.get(priority, 0.80)
    return b["used"].get(name, 0) + est <= cap


def _spend(name: str, tokens: int):
    from .persist import save_json
    b = _budget_today()
    b["used"][name] = int(b["used"].get(name, 0)) + max(0, int(tokens))
    save_json(_BUDGET_PATH, b)


def _chain(task: str) -> list[str]:
    return ROUTING.get(task, ROUTING["default"])


def _call(name: str, system: str, user: str, want_json: bool, max_tokens: int,
          temperature: float, timeout: float):
    """One provider call. Returns (content, tokens_used, rate_limited)."""
    p = PROVIDERS[name]
    key = os.getenv(p["key"])
    model = os.getenv(p["model_env"], p["model"])
    body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if name == "openrouter":
        headers["HTTP-Referer"] = "https://tentacle.bot"
        headers["X-Title"] = "TENTACLE"
    for _ in (1, 2):                                       # retry once without json-mode if rejected
        try:
            r = requests.post(p["url"], json=body, timeout=timeout, headers=headers)
        except Exception:
            return None, 0, False
        if r.status_code == 429:
            return None, 0, True
        if r.status_code == 400 and "response_format" in body:
            body.pop("response_format")
            continue
        if r.status_code != 200:
            return None, 0, False
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            used = (data.get("usage") or {}).get("total_tokens", max_tokens)
            return content, used, False
        except Exception:
            return None, 0, False
    return None, 0, False


def ask(system: str, user: str, want_json: bool = False, max_tokens: int = 1024,
        temperature: float = 0.3, timeout: float = 30.0, priority: str = "normal",
        task: str = "reason") -> str | None:
    """Route to the best available provider for `task`, failing over on missing key / 429 / over-budget."""
    if not _HAVE:
        return None
    est = max_tokens + len(user) // 4
    now = time.time()
    for name in _chain(task):
        if not _has_key(name) or _COOLDOWN.get(name, 0) > now:
            continue
        if PROVIDERS[name]["free"] and not _budget_ok(name, priority, est):
            continue
        content, used, limited = _call(name, system, user, want_json, max_tokens, temperature, timeout)
        if limited:
            _COOLDOWN[name] = now + 300                    # rest a rate-limited provider for 5 min
            continue
        if content is not None:
            if PROVIDERS[name]["free"]:
                _spend(name, used or est)
            return content
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
