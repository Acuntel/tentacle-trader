# EARSHOT — Master Prompt / Binding Spec

> Working codename: **EARSHOT** ("we just need to be in the know"). Rename freely.
> Owner: Brady Schwartz. Started 2026-06-18. This document is the single source of truth — when code and this doc disagree, fix the code.

---

## 0. North Star (one sentence)

**Stay in earshot of the whole market for free, and when a big, real, multi-source-confirmed catalyst lands on a stock that historically keeps drifting *up* after such news, automatically buy it — sized by how confident we are — then learn from every outcome and get sharper over time.**

We are **not** trying to win millisecond races (we'd lose — see §2). We are trying to be *early enough* on catalysts whose move **persists for hours to days**, and to **only act when the odds are heavily stacked.** Doing nothing for weeks is a valid, expected output.

---

## 1. The honest premise (read before anything)

- **Nothing is "guaranteed" to go up.** No filing, no flow, no headline. This system does not promise wins. It promises *discipline*: rare entries, stacked confirmation, hard-capped downside, and a learning loop that kills what doesn't work. The kill switch and the confidence gate **are** the strategy.
- **Everything we do is 100% legal.** We trade only on **public** information — public filings, public newswires, public flow, public social posts. We never act on material non-public information (an actual insider tip). Aggregating public data faster/smarter than the crowd is exactly what's allowed.
- **Capital is small and real ($500).** Treat every dollar as if it's the last one. Small size is a feature while we learn.

---

## 2. Ground truth & hard constraints (verified 2026-06-18)

**Robinhood account routing — non-negotiable:**
| Account | Type | Agent-tradeable? | Options | Balance | Use |
|---|---|---|---|---|---|
| **••8218 "Agentic"** | Cash | ✅ `agentic_allowed: true` | ❌ Level 0 | ~$500 (pending) | **THE bot account** |
| ••2994 | Margin | ❌ `agentic_allowed: false` | ✅ Level 2 (puts) | ~$106 | Manual only |

- The bot can **only** touch ••8218 → **equities + ETFs, long-only, no options, no crypto** (RH agentic is equities-only beta; no crypto execution tool exists).
- **Shorts** (later versions): the agent account can't buy puts → bearish view = **inverse ETFs** (SQQQ/SPXU/SOXS/SDOW). **v1 is long-only — skip shorts entirely.**
- **Cash-account settlement:** funds settle T+1; reselling unsettled proceeds = Good-Faith Violation. The risk module **must** track settled vs. unsettled cash and never trigger a GFV. (Upside: cash accounts are **exempt from PDT** — no $25k / 4-day-trade limit.)
- **Robinhood is a PFOF venue** (routes to Citadel/Virtu/IMC), publishes **no latency or slippage data**. Assume retail fills land *after* the initial spike. **This is why we only trade drift-persistent catalysts.**

---

## 3. Strategy in one diagram

```
   FREE SIGNAL FIREHOSE (§4)                 BRAIN (§5–§7)                EXECUTION (§9)
 ┌───────────────────────────┐      ┌──────────────────────────┐    ┌──────────────────┐
 │ Primary filings (EDGAR 8-K)│      │ 1. Catalyst classifier   │    │ RH MCP →         │
 │ Newswire PR (GlobeNewswire)│─────▶│    (earnings/FDA/index/M&A)│──▶│ buy equity/ETF   │
 │ Benzinga (free Alpaca WS)  │      │ 2. Confidence score 0–100 │    │ into ••8218      │
 │ News-API aggregators       │      │    (weighted multi-source)│    │ LONG ONLY        │
 │ Social velocity (ApeWisdom,│      │ 3. Risk gate + kill switch│    └──────────────────┘
 │   StockTwits, Reddit, X)   │      │ 4. Dynamic position size  │             │
 │ Flow (UW free pings/paid)  │      └──────────────────────────┘             ▼
 └───────────────────────────┘                    │               ┌──────────────────┐
                                                   └──────────────▶│ LEARNING LOOP §8 │
                                                                   │ log → outcome →  │
                                                                   │ re-weight sources│
                                                                   └──────────────────┘
```

**Fire only if ALL are true:**
1. ≥ **2 independent sources** confirm the **same** catalyst on the **same** ticker, within a tight time window.
2. Catalyst is a **drift-able category** (§6) — not a spent move.
3. Price hasn't already run past the **earliness threshold** since the source timestamp.
4. Direction = **up**, and we're **not already in that name**.
5. Inside all risk caps and the system is **ARMED**.
Else → log as a *shadow* signal (§8) and do nothing.

---

## 4. The FREE-FIRST signal stack (this is the "outside the box" part)

Tiered by trust. **Primary sources beat media beat social.** Everything below is free or has a usable free tier unless marked 💲. Each carries a **reliability weight** that the learning loop tunes over time (§8).

### Tier 1 — Primary sources (you're AT the source, often *ahead* of the news)
| Source | What it gives | Access | Latency | Notes |
|---|---|---|---|---|
| **SEC EDGAR** 8-K / Form 4 / 13D | Material events (M&A, bankruptcy, exec/guidance, insider buys, activist stakes) — the *legal primary document* | Free RSS (`/cgi-bin/browse-edgar?action=getcurrent`) + full-text API (`efts.sec.gov`) | Real-time on filing | **Highest weight.** An 8-K hits EDGAR before most media reports it. |
| **Newswire press releases** (GlobeNewswire, Business Wire, PR Newswire, Accesswire, Ga) | The company's own announcement (earnings, deals, FDA, contracts) | Free RSS feeds per wire | Real-time | This is the actual primary text Benzinga repackages. |
| **openFDA + FDA press RSS** | Drug approvals, PDUFA outcomes, recalls | Free API (`api.fda.gov`) + RSS | Minutes | Binary biotech catalysts. |
| **Federal Register API** | Regulatory actions, gov contracts | Free JSON API | Same-day | Niche but clean. |
| **S&P DJI / Nasdaq index announcements** | Index add/delete (forced fund buying → mechanical drift) | Free (press pages, after close) | After-close | One of the most *reliable directional* catalysts. |

### Tier 2 — Real newswire + free news APIs (structured, fast, free tiers)
| Source | What | Access | Notes |
|---|---|---|---|
| **Alpaca Benzinga WebSocket** | Real Benzinga newswire, structured JSON (`symbols[]`, headline, ts) | **Free** (`wss://stream.data.alpaca.markets/v1beta1/news`) | The backbone live trigger. |
| **Finnhub** 💲free-tier | Company news, **earnings surprises**, recommendation trends, earnings calendar | Free API key (60/min) | Best free earnings-beat data. |
| **Financial Modeling Prep** 💲free-tier | Press releases, earnings calendar, news | Free key | Redundant confirmation. |
| **Alpha Vantage NEWS_SENTIMENT** | News with **sentiment + relevance scores** per ticker | Free key (25/day) | Pre-scored sentiment. |
| **GDELT** | Real-time global news/event firehose, queryable | **Free**, no key | Outside-the-box wide net. |
| **Google News RSS** (per ticker) | Catch-all media coverage breadth | **Free** | Breadth/confirmation count. |

### Tier 3 — Social & community "the crowd now knows" velocity (confirmation, never sole trigger)
| Source | What | Access | Reality check |
|---|---|---|---|
| **ApeWisdom** | Free API of **Reddit/StockTwits mention counts + rank deltas** per ticker | **Free** (`apewisdom.io/api`) | Cleanest free "retail is piling in" velocity signal. |
| **StockTwits** | Trending tickers + tagged bull/bear sentiment | Free API (rate-limited) | Real-time crowd tag. |
| **Reddit** (r/wallstreetbets, r/stocks, r/options) | Mention spikes, DD threads | Free API (OAuth, rate-limited) | Velocity > content. |
| **X / Twitter fast-news accounts** | Headline relays — **@DeItaone (Walter Bloomberg)**, **@FirstSquawk**, **@unusual_whales**, **@Stocktwits** | ⚠️ Free X API read is crippled. Path = **browser agent** (claude-in-chrome) watching a pinned X **list/TweetDeck column**, or a Telegram mirror of these feeds | Fragile + ToS-gray. Treat as *bonus* confirmation, low weight, never required. |
| **Free Discord alert servers** (flow/news pings) | "People already doing this" | A **Discord bot token** (free) reading channels in servers we're invited to | Many free options/news Discords ping in real time. Low weight, fragile. |
| **Free Telegram channels** | Fast headline broadcasts | A Telegram **user-bot** reads public channels | We already run Telegram bots — easy to add. Mirror X feeds here too. |

### Tier 4 — Smart-money flow (FREE only)
| Source | What | Access | Notes |
|---|---|---|---|
| **Unusual Whales — free** | Public flow/Congress pings on **@unusual_whales X** + free site teasers + free Discord | Free (via Tier-3 path) | Free taste of flow. |

> **100% free (Brady, 2026-06-18) — the paid UW API is OUT.** The full free smart-money/whale stack (EDGAR 13D/Form 4/13F-Buffett, Congress, gov contracts, prediction markets, AI/quantum radar, alt-data) lives in **§18**.

> **Design rule:** the trigger is a Tier 1–2 primary catalyst (or a strong §18-A/B source). Social / flow-pings / alt-data raise or lower the **confidence score** and **size** — they rarely fire a trade alone. A social-only spike with no primary catalyst = pump risk → **down-weight hard.**

---

## 5. Catalyst classifier (§ what kind of news is this?)

Every incoming item is classified into a category, each with a learned **base drift prior** and **hold horizon**:

| Category | Why it drifts (retail-capturable) | Default direction | Default hold |
|---|---|---|---|
| **Earnings beat + raised guidance** | Post-earnings-announcement drift (PEAD) — one of finance's most documented anomalies | Long | 1–10 days |
| **S&P 500 / index inclusion** | Index funds *must* buy into the effective date | Long | days → effective date |
| **FDA / PDUFA approval** | Binary resolution often continues for days | Long | 1–5 days |
| **M&A — target company** | Jumps toward deal price; residual spread to capture | Long | until close/known |
| **Major contract / partnership / activist 13D** | Re-rating, slower bleed-in | Long | 1–10 days |
| **(v2) Earnings miss / guide-down / dilution / FDA reject** | Drifts *down* | Short (inverse ETF) | — **disabled in v1** |

Anything that doesn't classify cleanly → **no trade** (log as shadow).

---

## 6. Earliness & quality filters (don't buy the top)

- **Earliness:** if the stock has already moved more than `MAX_RUNUP` (default **+4%** intraday, category-tunable) since the source timestamp, the easy move is gone → skip or shrink.
- **Liquidity:** min avg dollar-volume + max bid/ask spread, so a $500 order doesn't eat slippage.
- **No-chase:** never buy more than `MAX_EXTENSION` above VWAP.
- **One catalyst per name per window:** dedupe so 5 headlines about the same event = 1 trade, not 5.

---

## 7. Confidence score → dynamic position size (the system chooses the dollar amount)

**Brady has authorized this system to size each trade itself, $0–$500, based on the model below.**

**Confidence score (0–100):**
```
raw   = Σ_sources ( reliability_weight_i × signal_strength_i )      # multi-source, weighted
score = clamp( raw
               × catalyst_prior_multiplier        # learned per category (§5)
               × magnitude_factor                 # earnings beat %, deal premium, etc.
               × earliness_factor                 # 1.0 if early, decays with run-up (§6)
               − crowding_penalty                 # if it's already a crowded/pump name
               − staleness_penalty , 0, 100)
```
- `reliability_weight_i` and `catalyst_prior_multiplier` **start at hand-set priors (§ below) and are tuned by the learning loop (§8).**
- **Independence matters:** two sources that are really the *same* wire echoing each other count as ~one. Group correlated sources.

**Score → action (v1 defaults, all tunable + learned):**
| Score | Action | Size (of $500) |
|---|---|---|
| < 55 | **No trade** — shadow-log only | $0 |
| 55–69 | Small | ~$100 |
| 70–84 | Medium | ~$225 |
| 85–94 | Large | ~$350 |
| 95–100 | Max conviction | up to **$500** |

**Hard caps over the top of sizing (cannot be overridden by score or learning):**
- **1 open position at a time in v1** (concentration is the point at this size; revisit later).
- Per-trade hard **stop-loss** (default −8%) and **profit target / trailing exit** per category hold.
- **Daily max-loss kill switch** (default −$100) → disarm for the day.
- **Repeated-execution auto-disarm** (FIA): after N entries in a window → halt until Brady re-arms.
- Never exceed **settled cash**; never cause a Good-Faith Violation.

**Starter priors (before learning kicks in):** EDGAR 8-K / newswire PR = weight **1.0**; Finnhub/FMP/Alpaca news = **0.8**; FDA/index official = **0.9**; GDELT/Google breadth = **0.4**; ApeWisdom/StockTwits/Reddit velocity = **0.35**; X fast-accounts = **0.3**; free Discord/Telegram pings = **0.25**; UW paid flow = **0.9**.

---

## 8. Recursive learning loop (gets smarter every trade)

This is a first-class requirement, not an add-on. **Two modes:**

### Phase A — SHADOW (paper) mode first
- The full pipeline runs live, but instead of buying it **records every candidate** — the ones it *would* trade **and** the sub-threshold ones — with their features (sources, scores, catalyst, timestamps, price).
- At each candidate's hold horizon, it logs the **hypothetical outcome** (P&L if we'd bought).
- This builds a real, money-free dataset fast and reveals: which sources actually precede up-moves, which catalysts drift, whether score predicts win-rate.
- **Promotion gate:** go live only after shadow mode shows, over a minimum sample, a hit-rate and expectancy above a set bar. *(This is also the honest answer to "is it ready to trade" — no, until shadow data earns it.)*

### Phase B — LIVE, still learning
- Every real trade is logged with full features + realized outcome.

### What it learns (persisted in `knowledge.json`, bounded + auditable)
1. **Per-source reliability** — when source *i* confirmed, how often did the trade work? → nudges `reliability_weight_i`.
2. **Per-catalyst stats** — realized hit-rate, average drift, **optimal hold time** → updates `catalyst_prior_multiplier` and default holds.
3. **Score calibration** — does "score 85" actually win ~85% of comparable horizons? Recalibrate the score→size map against a reliability curve.
4. **Earliness/run-up curve** — how much prior run-up kills the edge, per category.

### Update rules (safe by construction)
- Online blend: `weight ← (1−η)·weight + η·realized_signal`, small learning rate `η`.
- **Minimum sample size** before trusting any learned weight (no overfitting to 1 lucky trade).
- **Bounded weights** (e.g., [0.1, 1.2]); learning can tune within rails but **cannot** disable caps, the kill switch, or the long-only/account constraints.
- Everything logged append-only for audit; weekly summary pushed to Brady ("this week: X shadow signals, source EDGAR hit 71%, social-only signals hit 38% → down-weighted").

---

## 9. Execution path

- Target account **••8218** via the **Robinhood MCP** (`place_equity_order`, `review_equity_order`).
- **Long only:** buy the underlying stock or, for index/sector views, the long ETF.
- Marketable-limit orders (limit with a small band) — never naked market orders into a spike.
- **Pre-trade `review_equity_order`** to validate before `place_equity_order`.
- **Executor is an interface** with two backends so the brain never changes:
  - `RobinhoodMCPExecutor` (sanctioned agentic path — preferred).
  - `RobinStocksExecutor` (standalone fallback if headless/scheduled runs can't keep the MCP alive).
- **Step 0 of the build:** test whether a scheduled/headless run retains the RH MCP auth → picks the backend.

---

## 10. Architecture / file layout

```
news-whale-trader/
  signals/
    edgar.py            # SEC 8-K/Form4/13D RSS + full-text  (Tier 1)
    newswire.py         # GlobeNewswire/BusinessWire/PRNewswire RSS (Tier 1)
    fda.py              # openFDA + FDA RSS (Tier 1)
    news_alpaca.py      # free Benzinga WebSocket — primary live trigger (Tier 2)
    news_aggregators.py # Finnhub / FMP / AlphaVantage / GDELT / GoogleNews (Tier 2)
    social.py           # ApeWisdom / StockTwits / Reddit velocity (Tier 3)
    social_browser.py   # claude-in-chrome / Telegram-mirror of X fast accounts (Tier 3)
    discord_listener.py # free Discord/Telegram alert ingestion (Tier 3)
    flow_uw.py          # Unusual Whales free pings now / paid API later (Tier 4)
  catalysts.py          # classify item → category + magnitude (§5)
  conviction.py         # multi-source confidence score 0–100 (§7)
  sizing.py             # score → dollar size + caps (§7)
  risk.py               # stops, daily kill switch, settled-cash/GFV guard, repeated-exec disarm, ARMED toggle (§2/§7)
  executor.py           # interface + RobinhoodMCP / robin_stocks backends (§9)
  learning.py           # shadow logging, outcome scoring, knowledge.json updates, calibration (§8)
  main.py               # market-hours loop, dedupe, audit log, push alerts to Brady
  knowledge.json        # learned weights + per-source/catalyst stats (the memory)
  shadow_log.jsonl      # every candidate + outcome (the training set)
  trades_log.jsonl      # every real order + outcome
  config.yaml           # all thresholds, weights, caps (one place to tune)
  .env                  # API keys + RH creds — GitHub Secrets, never committed
```

---

## 11. Build phases (milestones — checkpoint state after each per Brady's rule)

1. **P0 – Plumbing & execution test.** Repo, config, RH MCP read + `review_equity_order` dry-run from a scheduled context → choose executor backend. *No live orders.*
2. **P1 – Brain, dry.** `catalysts.py` + `conviction.py` + `sizing.py` + `risk.py` against canned events. Unit-tested.
3. **P2 – Free signal feeds.** EDGAR + newswire + Alpaca Benzinga WS + Finnhub + ApeWisdom wired into the brain. Live data, **shadow mode only**.
4. **P3 – Shadow run.** Accumulate shadow signals + outcomes; weekly learning report. Tune until the promotion gate (§8) is met.
5. **P4 – Go live, armed, tiny.** Flip `ARMED`, real orders into ••8218 with the full kill-switch stack. Start at the smallest size band regardless of score for the first N trades.
6. **P5 – Expand.** Add §18 alt-data (Polymarket/Kalshi, USASpending, ClinicalTrials, AI/quantum radar), social/X/Discord layers via the Source Scout, then (v2) short side via inverse ETFs.

---

## 12. Open decisions for Brady
- **Free Discord/Telegram channels:** which are you already in / will you join? I'll research candidates and wire ingestion to the ones you approve.
- **X path:** browser-agent on your logged-in X List, or Telegram-mirror (recommended), or skip X for v1?
- **Ladder thresholds (§15):** accept the default Mastery bars / capital rungs, or tune them?
- **Project home** confirmed at `~/dev/news-whale-trader` (off iCloud, like StolenMule/Counsel)?

---

## 13. Status (the honest line, kept current)
**NOT LIVE. NOT TRADING. NO CODE BUILT YET.** 100% free sources. Accounts untouched. No real dollar moves until the bot clears the **Backtest Gauntlet → Live Shadow** rungs and earns Mastery ≥ 85 (§15). Full $500 access only at Mastery ≈ 92 sustained. Building begins at the Gauntlet on Brady's go.

---

## 14. The Survival Ethos — how the bot *values* the money (Brady's "mother & baby")

The capital is the **child**; the bot is its **keeper**; growing it = thriving, losing it = harm, zeroing it = **death**. Implemented as an **asymmetric, capital-preservation-first objective** the bot optimizes — *on top of* hard limits it can never override.

- **Loss aversion (losing hurts more than winning feels good):** the reward function weights losses ~2.5× heavier than equal gains (λ≈2.5). Formally it maximizes **log-growth of capital with a hard ruin floor** (Kelly-style, ruin-averse) — math for "grow the baby for the long run, never bet its life."
- **Death is real & terminal:** if equity hits the **ruin floor** (e.g. −40% of the stage's starting capital, tunable), the bot is **DEAD** — halts all trading, cannot restart itself, and only returns by **re-qualifying from a lower ladder rung** (§15). Losing the money costs it its access. *(In practice the most that can ever be lost is the small live balance; "death" is the access-revocation mechanic.)*
- **Sadness = the Dungeon (§16):** every losing trade triggers a forced written post-mortem before it may trade again.
- **Emotion guides, law binds:** the survival drive shapes *choices*, but the kill switch, per-trade stop, daily-loss halt, settled-cash rule, and ladder caps are enforced **outside the model** — the bot cannot hope, average-down, or reason its way past them.

## 15. The Crucible & the Graduation Ladder — earning access to the money

> Directly answers *"what score gives it full access."* Nothing real is risked until it proves mastery; capital is released **in rungs**, and **drawdowns demote it.**

**Mastery Score (0–100)** — composite over a rolling window of *resolved* signals:
```
Mastery = w1·ProfitFactor + w2·WinRate + w3·(1−MaxDrawdown) + w4·Calibration + w5·SampleSize + w6·Expectancy
```
- **Calibration** = does stated confidence match reality (a "95" should win ~95% of comparable horizons)? Overconfidence is punished.
- **SampleSize gate:** a high score on 3 trades counts for nothing — minimum N per rung.

**The Ladder (capital released only by sustained Mastery):**
| Rung | Stage | Capital live | Promote when | Demote if |
|---|---|---|---|---|
| **−1** | **Backtest Gauntlet** | $0 | Profitable across *years* of historical catalysts, out-of-sample, multiple regimes; Mastery ≥ **80** | fail → rebuild |
| **0** | **Live Shadow/Paper** | $0 | ≥ N_paper resolved *forward* signals (no hindsight), Mastery ≥ **85**, maxDD < cap | below bar → stay |
| **1** | **Micro** | **$25** | ≥ N green trades, Mastery ≥ 85 sustained | breach drawdown floor → down a rung |
| **2** | Small | **$75** | sustained Mastery ≥ 87 | ″ |
| **3** | Half | **$250** | sustained Mastery ≥ 90 | ″ |
| **4** | **Full** | **$500** | **Mastery ≥ 92 sustained over the longest window** | ″ |

- **Full-access answer: Mastery ≈ 92+ held over a long window with low drawdown** — full capital is the *top rung, not the default*.
- **Demotion is automatic:** breach a rung's drawdown limit → drop a rung, position cap shrinks with it. The keeper earns trust back.
- It may *always* trade less than its rung allows. Small or no trades are fine — it optimizes growth-with-survival, not activity (1/day or 1000/day, its call).

## 16. The Dungeon — learning from loss (forced reflection)

After **every losing trade** (and every losing shadow signal), before the next trade, the bot writes a structured post-mortem to `dungeon/post_mortems.jsonl`:
- Which sources it trusted + their weights at the time.
- Expected vs. actual (entry/exit/horizon/P&L).
- Root-cause hypothesis: late entry? bad source? wrong catalyst class? crowded/pump? regime shift?
- One concrete fix: down-weight a source, tighten an earliness filter, blacklist a setup.
- Updated belief → written to `knowledge.json`.

Weekly it self-summarizes ("my 3 worst losses were all social-only biotech triggers → biotech now requires a Tier-1 confirmation"). Real, auditable learning-from-pain — the productive version of "it gets sad."

## 17. The Source Scout — full-internet, self-improving data discovery

The bot is **not limited to a fixed source list.** `scout.py`:
- Scores each source's live **precision + lead-time** via the learning loop.
- When a source **decays** or coverage is thin on a hot theme (AI/quantum), uses **web search + fetch** to hunt new free sources — RSS feeds, free APIs, fast X/Discord/Telegram channels, alt-data endpoints — and proposes them.
- New sources enter at **probationary low weight, shadow-only**; they earn influence by track record. (Auto-add low-risk; human-approve anything needing a login.)
- This is "go find better info if what we have is shitty" — bounded so a bad new source can't hurt the baby.

## 18. The Expanded FREE Source Universe — unexpected places whales, Congress & Buffett move first

All free. The **paid** UW API is **OUT**. Grouped by *kind of edge*; weighting law at the bottom.

**A. The actual large movers (smart money) — free & primary:**
- **SEC EDGAR** real-time: **13D/13G** (activist & >5% stakes — these *pop* stocks), **Form 4** (insider cluster buys), **13F** (quarterly guru holdings — *track Berkshire/Buffett's CIK directly*), **Form 144** (insider sale intents), **8-K** (material events).
- **Free guru/whale trackers:** HedgeFollow, Stockcircle, GuruFocus (free), WhaleWisdom (free) — Buffett, Ackman, Burry, etc.
- **Congress:** Capitol Trades (free), House/Senate eFD PTR disclosures (free primary), @unusual_whales congress posts (free).

**B. Government & regulatory data that leads the news (free, unexpected):**
- **USASpending.gov API** — federal **contract awards**; a company landing a huge gov/defense contract is a catalyst visible in the data feed.
- **ClinicalTrials.gov API** — trial status/result changes precede biotech moves.
- **openFDA + FDA AdComm/PDUFA calendars** — drug decisions. **USPTO patents, FCC filings, Federal Register** — approvals/rulings. **CME FedWatch, BLS/BEA, FOMC calendar** — macro.

**C. Crowd-forecast & attention (free, often *ahead* of headlines):**
- **Polymarket / Kalshi APIs** — prediction-market odds on FDA/Fed/M&A/elections shift *before* official news. (Same DNA as your HOMECOURT.)
- **Google Trends (pytrends)** + **Wikipedia pageview API** — search/attention spikes on a product or company.
- **Hacker News (Algolia API)** — front-page = fast tech/AI/quantum catalysts.
- **GitHub Trending / Hugging Face Trending / Papers with Code / arXiv** — your **AI & quantum radar**: a model/paper/repo going viral moves related names (NVDA, semis, quantum tickers).
- **ApeWisdom / StockTwits / Reddit (WSB, r/stocks, r/options)** — retail velocity.

**D. Alt-data / "weird but real" (free-ish, low weight, fun):**
- **ADS-B Exchange (flight tracking)** — corporate-jet spotting around HQs has historically front-run M&A.
- **Job-posting velocity**, **app-store rank moves**, **Similarweb free web-traffic** — demand proxies. **EarningsWhispers** — whisper vs. actual EPS.

**E. Fast-relay social (free, fragile, low weight, confirmation-only):**
- **X fast accounts** — @DeItaone (Walter Bloomberg relays Bloomberg headlines free), @FirstSquawk, key political accounts (tariff/policy headlines). **No clean free X API** → robust free path = a **browser agent on your logged-in X watching a curated List**, *or* mirror those accounts into a **Telegram channel you own** and read Telegram (free + robust, recommended).
- **Free Discord/Telegram alert servers** — read by a free bot token in servers you've joined.

> **Weighting law:** A–B (primary/official) are high-weight and may *trigger*. C is medium — a great *lead* signal, confirm with A/B. D–E are low-weight **confirmation only** — they never trigger a real-money trade alone (pump/noise risk). **The baby is never risked on a tweet.**
