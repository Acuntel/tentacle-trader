# TENTACLE 🐙

A **free**, self-learning news/whale auto-trader that treats its capital like a child.
An octopus: many tentacles constantly scour the internet for catalysts; one brain scores
conviction across *independent* sources; a mother's instinct protects the money and earns
access to more only by proving itself.

> **STATUS: SHADOW MODE. No real money moves.** Live execution is hard-refused in code until
> the bot clears the Backtest Gauntlet, reaches a live rung, and is explicitly armed.
> Full binding spec: [`MASTER_PROMPT.md`](./MASTER_PROMPT.md).

## Run it

```bash
cd ~/dev/news-whale-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m earshot.main --once      # one cycle
python -m earshot.main --loop      # keep scouring
```

Runs with **zero API keys** — EDGAR, ApeWisdom, Hacker News, and any RSS feed need none.
Optional free keys (Alpaca/Finnhub/etc.) light up extra tentacles automatically.

## Configuration (.env — all optional, all free)

| Var | Lights up |
|---|---|
| `TENTACLE_USER_AGENT` | SEC EDGAR requires a contact UA (set to your email) |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | Alpaca free Benzinga newswire |
| `FINNHUB_API_KEY` | Finnhub news + earnings surprises |
| `ALPHAVANTAGE_API_KEY`, `FMP_API_KEY` | extra free news/sentiment |
| `REDDIT_*`, `TELEGRAM_BOT_TOKEN` | social tentacles |

Add RSS feeds (RSSHub / Nitter / Telegram-RSS / newswires / Google News) under `sources.rss_feeds`
in `config.yaml` — that generic tentacle is how we **route around X** for free.

## How it thinks

1. **Tentacles** (`sources/`) scour free feeds → `RawSignal`s.
2. **Brain** (`brain.py`) classifies the catalyst and scores **conviction** — it only fires when
   ≥2 *independent* sources agree, the move is still early, and the tape isn't crashing.
3. **MotherInstinct** (`survival.py`) feels losses ~2.5× harder than gains, climbs a **capital
   ladder** ($0 → $25 → $75 → $250 → $500) by earning a **Mastery score**, and **dies** if it
   breaches the ruin floor (then must re-qualify).
4. **RiskGate** (`risk.py`) is the hard law — kill switch, one-position cap, penny/pump filter —
   enforced *outside* the model so the bot can't rationalize past it.
5. **Learner** (`learning.py`) logs every candidate, scores the outcome, **re-weights sources** by
   their real track record, and after every loss writes a **Dungeon** post-mortem.
6. **Scout** (`scout.py`) watches tentacle quality and proposes new free sources when one decays.

## The path to real money
`Backtest Gauntlet → Live Shadow → Micro $25 → … → Full $500`, gated by Mastery ≥ 80→92.
Nothing real trades until that's earned and you arm it. See `MASTER_PROMPT.md` §15.
