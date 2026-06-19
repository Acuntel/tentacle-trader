# Deploying EARSHOT 24/7

Two runtimes, because of one hard constraint: **the Robinhood MCP (the sanctioned agentic
path) lives inside a Claude agent session — a standalone server can't reach it.**

## Option A — Railway worker (24/7, standalone)  ← what `Dockerfile`/`Procfile` target
Runs the loop forever in the cloud. Uses the **robin_stocks** executor for live trades, so it
needs your Robinhood creds as Railway secrets. **Still gated** — it won't place a real order
until the Gauntlet passes, a live rung is reached, and `armed=true`.

```bash
cd ~/dev/news-whale-trader
railway login
railway init                     # create/link a project
railway up                       # build the Dockerfile and deploy the worker
# add a persistent volume mounted at /app/state and /app/dungeon (the bot's memory)
railway variables set EARSHOT_USER_AGENT="EARSHOT bot bschwartz1725@gmail.com"
# DO NOT set ROBINHOOD_* until you've decided to go live:
# railway variables set ROBINHOOD_USERNAME=... ROBINHOOD_PASSWORD=...
```

## Option B — Scheduled Claude agent (sanctioned RH MCP, no RH password)  ← recommended for live
Because trades are rare and conviction-gated, you don't need a millisecond daemon. A Claude
agent on a cron (e.g. every few minutes in market hours) runs one EARSHOT cycle and places any
approved order via the RH MCP into the agentic account ••8218 — the account built for exactly
this. No Robinhood password stored anywhere. This is the cleaner live path; Railway (Option A)
is the fallback if you want a pure standalone service.

## The gate (both options)
`state/gauntlet.json` must show `overall.mastery >= 80`, the ladder must have reached a live
rung (`state/ladder.json`), and live trading must be explicitly armed. Until then: shadow only.
Current Gauntlet: **FAIL (42/100)** — real money is locked.
