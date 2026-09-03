"""The reasoning layer — a POOL of free LLMs working in unison, each routed to what it's best at.

Every provider here is OpenAI-chat-compatible, so one `_call` drives them all. For each task we try a
PREFERRED chain of providers (best-for-the-job first); if one is missing a key, rate-limited (429), or
over its daily budget, we fail over to the next. So losing any single provider (e.g. Groq hits its
100k-tokens/day free limit) doesn't stop the bot — the others cover. Per-provider daily token budgets
are tracked + persisted, and a PRIORITY reserve keeps the loop-critical callers (coach/proposer) from
being starved by the high-volume analyst.

All FREE (activate by setting the env key); paid ones (deepseek-direct, moonshot, xai) stay off unless set:
  GROQ_API_KEY        Groq — fast Llama, free 100k tok/day                  (US)
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
import random
import time

try:
    import requests
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

# name -> endpoint config. `daily` = soft daily TOKEN budget (the 429-cooldown is the real guard).
_NV = "https://integrate.api.nvidia.com/v1/chat/completions"   # NVIDIA NIM hosts MANY models on one URL
PROVIDERS = {
    "groq":       {"url": "https://api.groq.com/openai/v1/chat/completions",
                   "key": "GROQ_API_KEY", "model_env": "GROQ_MODEL",
                   "model": "llama-3.3-70b-versatile", "free": True, "daily": 95_000},
    # gemini REMOVED 2026-07-13: 0/2035 usable answers live (quota-dead key) — it was burning a
    # failover slot every cycle. To re-add, restore this block + its ROUTING entries once a real key works.
    "cerebras":   {"url": "https://api.cerebras.ai/v1/chat/completions",
                   "key": "CEREBRAS_API_KEY", "model_env": "CEREBRAS_MODEL",
                   "model": "gpt-oss-120b", "free": True, "daily": 1_000_000},        # ultra-fast, verified
    "cerebras_glm": {"url": "https://api.cerebras.ai/v1/chat/completions",
                   "key": "CEREBRAS_API_KEY", "model_env": "CEREBRAS_GLM_MODEL",
                   "model": "zai-glm-4.7", "free": True, "daily": 1_000_000},          # fast Chinese coder
    "sambanova":  {"url": "https://api.sambanova.ai/v1/chat/completions",
                   "key": "SAMBANOVA_API_KEY", "model_env": "SAMBANOVA_MODEL",
                   "model": "Meta-Llama-3.3-70B-Instruct", "free": True, "daily": 1_000_000},
    "nvidia":     {"url": _NV, "key": "NVIDIA_API_KEY", "model_env": "NVIDIA_MODEL",
                   "model": "meta/llama-3.3-70b-instruct", "free": True, "daily": 1_000_000},
    # NVIDIA-hosted models (all verified live) — distinct keys spread the free-tier quota across them
    "nv_kimi":     {"url": _NV, "key": "NV_KIMI_KEY", "model_env": "NV_KIMI_MODEL",
                    "model": "moonshotai/kimi-k2.6", "free": True, "daily": 500_000},         # strong coder
    "nv_nemotron": {"url": _NV, "key": "NVIDIA_API_KEY", "model_env": "NV_NEMOTRON_MODEL",
                    "model": "nvidia/nemotron-3-ultra-550b-a55b", "free": True, "daily": 200_000},  # big reasoner (slow)
    "nv_mistral":  {"url": _NV, "key": "NV_MISTRAL_KEY", "model_env": "NV_MISTRAL_MODEL",
                    "model": "mistralai/mistral-nemotron", "free": True, "daily": 500_000},   # agentic
    "nv_sarvam":   {"url": _NV, "key": "NV_SARVAM_KEY", "model_env": "NV_SARVAM_MODEL",
                    "model": "sarvamai/sarvam-m", "free": True, "daily": 500_000},            # math/quant
    "nv_stepfun":  {"url": _NV, "key": "NV_STEPFUN_KEY", "model_env": "NV_STEPFUN_MODEL",
                    "model": "stepfun-ai/step-3.7-flash", "free": True, "daily": 500_000},    # reasoning
    "nv_bytedance":{"url": _NV, "key": "NV_BYTEDANCE_KEY", "model_env": "NV_BYTEDANCE_MODEL",
                    "model": "bytedance/seed-oss-36b-instruct", "free": True, "daily": 500_000},  # fast
    "moonshot":   {"url": "https://api.moonshot.ai/v1/chat/completions",
                   "key": "MOONSHOT_API_KEY", "model_env": "MOONSHOT_MODEL",
                   "model": "kimi-k2-0711-preview", "free": False, "daily": 10 ** 9},
    "glm":        {"url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                   "key": "GLM_API_KEY", "model_env": "GLM_MODEL",
                   "model": "glm-4-flash", "free": True, "daily": 2_000_000},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions",
                   "key": "OPENROUTER_API_KEY", "model_env": "OPENROUTER_MODEL",
                   # PAID via the $10: DeepSeek is cheap (~pennies/1M) + a top coder. The :free routes are
                   # rate-limited, and we already have free Llama elsewhere — so spend it on strength.
                   # daily token cap keeps $10 lasting months; positioned for quality lanes, not volume.
                   "model": "deepseek/deepseek-chat", "free": False, "daily": 200_000},
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
# task -> preferred provider order. FAST models lead the loop-inline lanes (judge/reason/propose) so a slow
# reasoner is only reached on failover and never stalls the trading loop. Every model serves multiple tasks.
# FAST/FREE models lead high-volume lanes; OpenRouter→DeepSeek (paid, capped) sits in QUALITY lanes
# (code/reason/quant) not volume lanes (judge). Every model still serves multiple tasks.
# NOTE: a provider that goes fully dead (0 usable answers over a long window, per failing_parts) is now
# AUTO-QUARANTINED at routing time (see _quarantined) — a dead tail still costs a failover attempt each
# cycle, so _chain() drops it entirely (all lanes) and only re-probes it rarely so recovery stays possible.
ROUTING = {
    "judge":   ["cerebras", "groq", "sambanova", "nv_bytedance", "nvidia"],                          # fast, high-volume — NO paid
    "reason":  ["cerebras", "openrouter", "nv_mistral", "nvidia", "groq", "nv_nemotron", "nv_stepfun"],  # deep reasoning
    "code":    ["nv_kimi", "openrouter", "cerebras_glm", "nv_stepfun", "nv_nemotron", "glm", "qwen", "nvidia"],  # code-strong (Kimi/DeepSeek/GLM)
    "propose": ["cerebras", "openrouter", "groq", "nv_mistral", "sambanova"],                        # quality + evidence
    "quant":   ["nv_sarvam", "openrouter", "nv_nemotron", "nv_kimi", "cerebras_glm", "nv_mistral"],  # math / strategy synthesis
    "agentic": ["nv_mistral", "nv_kimi", "openrouter", "cerebras", "nvidia"],                        # tool-use / multi-step
    "default": ["cerebras", "groq", "sambanova", "nvidia", "nv_mistral", "openrouter"],
}
RESERVE = {"low": 0.50, "normal": 0.80, "high": 0.95}     # per-provider daily fraction a priority may use
_BUDGET_PATH = "state/llm_budget.json"
_STATS_PATH = "state/llm_stats.json"                      # LEARNED router: per (provider|task) track record
_COOLDOWN: dict[str, float] = {}                          # name -> epoch until which to skip (after a 429)
_stats_cache: dict | None = None
_QUAR_CALLS = 150                                         # lifetime calls with ZERO usable answers -> quarantine
_PROBE_EVERY = 200                                        # routed calls between probation probes of a quarantined provider
_PROBE_TASK = "judge"                                     # probe on the cheapest lane (fast, free, high-volume)
_route_count = 0                                          # routed calls this session (drives probation cadence)
_probe_at: dict[str, int] = {}                            # name -> _route_count at its last probation probe


# ---- THE LEARNED ROUTER — the pool stops being a hand-ordered guess and learns who's good at what ----
def _stats() -> dict:
    global _stats_cache
    if _stats_cache is None:
        from .persist import load_json
        _stats_cache = load_json(_STATS_PATH, {})
    return _stats_cache


def _save_stats():
    from .persist import save_json
    save_json(_STATS_PATH, _stats())


def _record(name: str, task: str, ok: bool, limited: bool = False, latency: float | None = None):
    d = _stats().setdefault(f"{name}|{task}", {"calls": 0, "ok": 0, "limited": 0, "lat": 0.0})
    d["calls"] += 1
    d["ok"] += 1 if ok else 0
    d["limited"] += 1 if limited else 0
    if latency is not None:
        d["lat"] = round(0.9 * d["lat"] + 0.1 * latency, 2) if d["lat"] else round(latency, 2)
    _save_stats()


def feedback(name: str | None, task: str, good: bool):
    """DOWNSTREAM quality signal — not 'did it answer' but 'was the answer actually good' (a self-code
    patch survived the test+fitness gate, JSON parsed, a review was actionable). This is what teaches
    the router which model truly codes/reasons best for US, from our own outcomes."""
    if not name:
        return
    d = _stats().setdefault(f"{name}|{task}", {"calls": 0, "ok": 0, "limited": 0, "lat": 0.0})
    d["graded"] = int(d.get("graded", 0)) + 1
    d["good"] = int(d.get("good", 0)) + (1 if good else 0)
    _save_stats()


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
    return (b["used"].get(name, 0) or 0) + est <= cap


def _spend(name: str, tokens: int):
    from .persist import save_json
    b = _budget_today()
    b["used"][name] = int(b["used"].get(name, 0)) + max(0, int(tokens))
    save_json(_BUDGET_PATH, b)


def _quarantined() -> set[str]:
    """AUTO-QUARANTINE (the gemini lesson: 0/2035 usable answers, yet it burned a failover attempt
    every escalation until a human deleted it). A provider whose LIFETIME stats across ALL lanes show
    >= _QUAR_CALLS calls and ZERO usable answers is dead (quota-dead key, gone model) — _chain() skips
    it entirely, all lanes, instead of just demoting. Reversible by DATA alone: one usable answer
    (a probation probe, or any manual call after a key rotation) and it re-enters automatically."""
    s = _stats()
    agg: dict[str, list[int]] = {}
    for key, d in s.items():
        if "|" not in key or not isinstance(d, dict):
            continue
        a = agg.setdefault(key.split("|")[0], [0, 0])
        a[0] += d.get("calls", 0)
        a[1] += d.get("ok", 0)
    quar = {n for n, (calls, ok) in agg.items() if n in PROVIDERS and calls >= _QUAR_CALLS and ok == 0}
    marker = {"names": sorted(quar)}                       # surfaced so selfreview/failing_parts can report "quarantined (auto)"
    if s.get("_quarantined") != marker:
        s["_quarantined"] = marker
        _save_stats()
        print(f"[llm] auto-quarantine: {', '.join(marker['names']) or '(none — all clear)'}", flush=True)
    return quar


def _chain(task: str) -> list[str]:
    """Preferred order for a task, RE-RANKED by measured performance: answer rate + downstream
    'was it actually good' grades, with the hand-tuned order as the prior for unproven providers.
    A 10% exploration swap keeps sampling non-leaders so a recovered/new provider can earn its way
    up (otherwise one early 429 streak buries a good model forever). Fully-dead providers are
    quarantined out entirely (see _quarantined), with a rare probation probe on the cheapest lane."""
    global _route_count
    _route_count += 1
    quar = _quarantined()
    base = [n for n in ROUTING.get(task, ROUTING["default"]) if n not in quar]
    s = _stats()

    def _score(pos: int, name: str) -> float:
        prior = 1.0 - pos * 0.02                          # hand order = the prior / tiebreak
        d = s.get(f"{name}|{task}")
        if not d or d.get("calls", 0) < 5:
            # unproven: assume NEUTRAL quality (~0.7) so a proven performer outranks it but a proven
            # failer sinks below it — same scale as the measured score, hand order breaks ties.
            return 0.63 + 0.10 * prior
        ok_rate = (d.get("ok", 0) + 1) / (d.get("calls", 0) + 2)          # laplace-smoothed
        graded = d.get("graded", 0)
        good_rate = (d.get("good", 0) + 1) / (graded + 2) if graded else ok_rate
        return 0.55 * ok_rate + 0.35 * good_rate + 0.10 * prior

    ranked = [n for _, _, n in sorted(((-_score(i, n), i, n) for i, n in enumerate(base)))]
    if len(ranked) > 2 and random.random() < 0.10:
        j = random.randrange(1, len(ranked))
        ranked[0], ranked[j] = ranked[j], ranked[0]
    # PROBATION: a quarantined provider gets ONE attempt per ~_PROBE_EVERY routed calls, cheapest lane
    # only, so recovery (e.g. a rotated key) needs no human. Front of the chain so the probe actually
    # fires; a dead one fails fast and normal failover continues — bounded cost, automatic re-entry.
    if quar and task == _PROBE_TASK:
        due = [n for n in sorted(quar) if _route_count - _probe_at.get(n, 0) >= _PROBE_EVERY]
        if due:
            _probe_at[due[0]] = _route_count
            ranked.insert(0, due[0])
    return ranked


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
            msg = data["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content")   # reasoning models reply in the latter
            used = (data.get("usage") or {}).get("total_tokens", max_tokens)
            return content, used, False
        except Exception:
            return None, 0, False
    return None, 0, False


def _ask(system, user, want_json, max_tokens, temperature, timeout, priority, task, avoid):
    """Core router. Returns (content, provider_name) — failing over on missing key / 429 / over-budget /
    a provider in `avoid` (so one model never reviews its own work)."""
    if not _HAVE:
        return None, None
    avoid = avoid or set()
    est = max_tokens + len(user) // 4
    now = time.time()
    # WALL-CLOCK CEILING (2026-09-02). `timeout` bounds ONE call, but this loop fails over across
    # the whole pool — 12 providers x a 30s timeout is a 6-minute worst case on a loop that ticks
    # every 10s, and the coach runs on the trading thread. The per-call timeout was never the
    # binding constraint; the chain was. Past the deadline we stop failing over and degrade to the
    # caller's fallback (distilled judge / heuristic), which every LLM caller here already has.
    deadline = now + max(float(timeout), float(os.getenv("TENTACLE_LLM_CHAIN_BUDGET", "45")))
    for name in _chain(task):
        if time.time() >= deadline:
            break
        if name in avoid:
            continue
        if not _has_key(name) or _COOLDOWN.get(name, 0) > now:
            continue
        timeout = min(timeout, max(1.0, deadline - time.time()))   # never overrun the ceiling
        if not _budget_ok(name, priority, est):           # daily token cap applies to ALL — caps paid spend too
            continue
        t0 = time.time()
        content, used, limited = _call(name, system, user, want_json, max_tokens, temperature, timeout)
        if limited:
            _COOLDOWN[name] = now + 300                    # rest a rate-limited provider for 5 min
            _record(name, task, ok=False, limited=True)
            continue
        if content is not None:
            _spend(name, used or est)                      # track spend for every provider (free + paid)
            _record(name, task, ok=True, latency=time.time() - t0)   # the router learns who delivers
            return content, name
        _record(name, task, ok=False)
    return None, None


def ask(system: str, user: str, want_json: bool = False, max_tokens: int = 1024,
        temperature: float = 0.3, timeout: float = 30.0, priority: str = "normal",
        task: str = "reason", avoid=None) -> str | None:
    """Route to the best available provider for `task`, failing over on missing key / 429 / over-budget."""
    return _ask(system, user, want_json, max_tokens, temperature, timeout, priority, task, avoid)[0]


def ask_meta(system: str, user: str, want_json: bool = False, max_tokens: int = 1024,
             temperature: float = 0.3, timeout: float = 30.0, priority: str = "normal",
             task: str = "reason", avoid=None) -> dict:
    """Like ask(), but also reports WHICH model answered — used for cross-model review."""
    c, name = _ask(system, user, want_json, max_tokens, temperature, timeout, priority, task, avoid)
    return {"content": c, "provider": name}


def ask_json(system: str, user: str, **kw) -> dict | None:
    task = kw.get("task", "reason")
    txt, name = _ask(system, user, True, kw.get("max_tokens", 1024), kw.get("temperature", 0.3),
                     kw.get("timeout", 30.0), kw.get("priority", "normal"), task, kw.get("avoid"))
    if not txt:
        return None
    try:
        out = json.loads(txt)
    except Exception:
        try:
            s, e = txt.index("{"), txt.rindex("}")
            out = json.loads(txt[s:e + 1])
        except Exception:
            feedback(name, task, False)                   # answered but unusable — a real quality miss
            return None
    feedback(name, task, True)                            # parseable structured output = a quality hit
    return out
