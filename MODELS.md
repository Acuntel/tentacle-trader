# TENTACLE — Model Registry & Access Resilience

The bot's "brain" is a **pool of free LLMs** (`earshot/llm.py`), each routed to what it's best at. This file
guarantees we **never lose access to a model** even if a host (e.g. NVIDIA) changes its free tier.

## The resilience principle

Every model we use is **open-weight**. We do NOT host the weights ourselves (they're 36B–550B params and need
GPUs we don't have) — instead the weights live **permanently on Hugging Face**, and we reach them through whichever
**free API host** serves them. If one host cuts free access, we re-point to another by changing one env var /
model string. The pool already **fails over** automatically across hosts within each task lane.

So "access" = (open weights on HF, never disappear) + (multiple interchangeable hosts) + (automatic failover).

## Live pool (verified reachable 2026-06-28)

| Model | Host (env var) | Endpoint / model id | Hugging Face weights | Backup hosts | Lanes |
|---|---|---|---|---|---|
| Llama-3.3-70B | Groq `GROQ_API_KEY` | `llama-3.3-70b-versatile` | meta-llama/Llama-3.3-70B-Instruct | SambaNova, NVIDIA, Cerebras, OpenRouter, Together | judge |
| Llama-3.3-70B | SambaNova `SAMBANOVA_API_KEY` | `Meta-Llama-3.3-70B-Instruct` | (same) | Groq, NVIDIA, OpenRouter | judge, propose |
| Llama-3.3-70B | NVIDIA `NVIDIA_API_KEY` | `meta/llama-3.3-70b-instruct` | (same) | Groq, SambaNova | judge, agentic |
| gpt-oss-120b | Cerebras `CEREBRAS_API_KEY` | `gpt-oss-120b` | openai/gpt-oss-120b | Groq, OpenRouter, Together, Fireworks | judge, reason, propose |
| zai-GLM-4.7 | Cerebras `CEREBRAS_API_KEY` | `zai-glm-4.7` | zai-org/GLM-4.6 (family) | Zhipu direct (`GLM_API_KEY`), OpenRouter | code |
| Kimi-K2.6 | NVIDIA `NV_KIMI_KEY` | `moonshotai/kimi-k2.6` | moonshotai/Kimi-K2-Instruct | Moonshot direct (`MOONSHOT_API_KEY`), OpenRouter | code, quant, agentic |
| Nemotron-3-Ultra-550B | NVIDIA `NVIDIA_API_KEY` | `nvidia/nemotron-3-ultra-550b-a55b` | nvidia/* (NIM) | (NVIDIA-specific) → fail over to Gemini/Mistral in lane | reason, quant |
| Mistral-Nemotron | NVIDIA `NV_MISTRAL_KEY` | `mistralai/mistral-nemotron` | mistralai/* | Mistral direct (La Plateforme), OpenRouter | reason, propose, agentic |
| Sarvam-M (math) | NVIDIA `NV_SARVAM_KEY` | `sarvamai/sarvam-m` | sarvamai/sarvam-m | (NVIDIA) → fail over in lane | quant, reason |
| StepFun-3.7-flash | NVIDIA `NV_STEPFUN_KEY` | `stepfun-ai/step-3.7-flash` | stepfun-ai/* | (NVIDIA) → fail over in lane | reason, code |
| ByteDance-Seed-OSS-36B | NVIDIA `NV_BYTEDANCE_KEY` | `bytedance/seed-oss-36b-instruct` | ByteDance-Seed/Seed-OSS-36B-Instruct | OpenRouter | judge |

### Embeddings & optimization (not chat — wired into dedicated modules)
| Tool | Host (env var) | Endpoint / id | Use in the bot |
|---|---|---|---|
| BGE-M3 (1024-dim) | NVIDIA `NV_BGE_KEY` | `baai/bge-m3` (`/v1/embeddings`) | semantic dedup of news/signals + RAG over the brain |
| cuOpt | NVIDIA `NV_CUOPT_KEY` | cuOpt numerical-optimization API | portfolio weight optimization / strategy-param search |

## Dark until keyed (set the env var to activate — already wired)
- `OPENROUTER_API_KEY` — **the resilience gateway**: one key reaches DeepSeek, Qwen, GLM, Kimi, Llama, gpt-oss
  (many `:free`). ⚠️ needs ~$5 one-time credit to unlock free routes (account currently uncredited).
- `GEMINI_API_KEY` — best free reasoner; need a real `AIza…` key (the `AQ.…` token 429'd).
- `GLM_API_KEY` (Zhipu), `DASHSCOPE_API_KEY` (Qwen), `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY` — direct CN hosts (backups).

## If NVIDIA cuts free access tomorrow
1. The pool **auto-fails-over** to the backup hosts above within each lane — no outage.
2. For NVIDIA-only models (Nemotron/Sarvam/StepFun), the lane degrades to the next model — still functional.
3. To restore a specific model elsewhere: set that host's env var + model id (this table has the ids). Done.
4. Full mirrors of the NVIDIA **skills** repo and the **quant agent** repo are preserved under `Acuntel/` on
   GitHub, so the *code/docs* survive regardless of NVIDIA.

## Skipped (verified non-working 2026-06-28)
- `deepseek-ai/deepseek-v4-pro` (NVIDIA) — times out >90s, useless in-loop. Use DeepSeek via OpenRouter/direct.
- `qwen/qwen3.5-397b-a17b` (NVIDIA) — times out >90s. Use Qwen via OpenRouter/DashScope.
