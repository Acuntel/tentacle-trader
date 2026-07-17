# EDGE MASTERPROMPT — the doctrine that everything serves

> 2026-07-17. This file is the single source of truth for WHERE Tentacle's edge comes from.
> Every subsystem — brain, learner, Forge, Hive, self-code, the engineer loop — optimizes for
> THIS. Anything that doesn't serve it is a candidate for deletion. Rails (risk law, ladder,
> Gauntlet, shadow mode, real-money gate) are UNTOUCHED by this doctrine and stay locked.

## The two structural facts (they decide everything)

1. **Latency is ~minutes-to-hours** (30-min-class polling → LLM scoring → next-cycle entry).
   Any signal priced in within minutes — breaking newswire, big-cap FDA/M&A pops, covered-name
   earnings — is a race we structurally LOSE to millisecond players. 811 explore trades at 44%
   dir-hit and -6.1% alpha is what buying-after-the-move looks like. Signals are harvestable
   ONLY when their diffusion half-life (days→weeks) dwarfs our latency.
2. **Size is $25–500.** No market impact; we can own microcap drift trades no fund can enter
   without moving the price. The anomalies that persist are exactly the ones that don't scale.
   Small size is the moat, not the weakness.

**Therefore: the edge is slow-diffusion event drift in small / under-covered names, long,
held for days-to-weeks.** Edge = information × (1 − competition).

## The family doctrine

- **TRIGGER families** (slow-diffusion information; a conviction setup MUST include ≥1):
  `filing` (8-K, Form 4 clusters, 13D), `regulatory` (gov contracts, FDA, index inclusion),
  `surprise` (earnings beats → PEAD drift), `demand` (Steam/app-rank/reviews/downloads),
  `flow`, `alt` (hiring velocity).
- **CONFIRM families** (corroborate, can be the 2nd family, but can NEVER anchor a setup):
  `news`, `attention`, `onchain`, `other`. By the time fast news is the only evidence, the
  move belongs to someone faster.
- **ANTI families** (they measure COMPETITION, not information): `social`, `crowd`. They never
  count toward min_families and their presence PENALIZES the score. The learner proved this
  itself: every vetoed pattern contains `+social`. When Reddit confirms, we're late.

## Relative surprise, not absolute size

Score every event by how big it is FOR THAT COMPANY: contract $ ÷ market cap, insider buy vs
that insider's history, review-velocity z-score. A $50M award to a $100M-cap company is a
thesis; a $2B award to Lockheed is a rounding error.

## The catalyst graduation ladder (how an edge gets PROVEN, per family)

Every catalyst keeps a forward ledger (patterns_agg, catalyst level — resolved out-of-sample
outcomes only). Judged against the live field rate (~0.34 alpha-win base):

- **GRADUATED** — n ≥ 30 AND Wilson-lower(win rate) > field rate. Live conviction entries are
  restricted to graduated catalysts only; in shadow, graduated catalysts get a score boost.
- **PENDING** — everything else keeps earning explore samples.
- **DEAD** — n ≥ 50 AND Wilson-upper(win rate) < field − 0.05. Explore budget stops flowing to
  it (hard vetoes already handle per-signature losers; this kills the whole class).
- `unknown` is exempt both ways: it can never graduate and is never killed (it's the discovery
  pool, not a catalyst).

**The headline question changes from "is the pooled model calibrated?" to "how many catalysts
have graduated?" One graduated catalyst = a real, provable, out-of-sample edge.**

## The errors this doctrine fixed (2026-07-17 build)

1. **Demand data had no catalyst.** Steam/app-rank/reviews/downloads — the scout's top-scored,
   most-differentiated sources — emitted no `catalyst_hint`, landed in `unknown`, defaulted to
   a 5-day hold. → New `demand_surge` catalyst, 15-day thesis hold (`sources/demand.py`,
   `sources/web.py`, `sources/adoption.py`, `models.py`, `config.yaml`).
2. **Social/crowd counted as confirming families** and fed the convergence bonus. → ANTI
   doctrine in `brain.py`: excluded from family count, crowding penalty −8%/social source
   (floor 0.70). Signatures still record them so the learner keeps seeing the pattern.
3. **News could anchor a setup** (news+attention = 2 families, zero slow information). →
   conviction now requires ≥1 TRIGGER family; trigger-less multi-family names fall into the
   bounded near-miss learning lane (the doctrine stays falsifiable).
4. **gov_contract strength scaled by ABSOLUTE dollars** (`0.6 + amt/1e9`): Lockheed's $2B
   rounding error out-scored a microcap's transformative $50M award 3×. → relative surprise:
   `amt / market_cap` when fundamentals are cached, absolute fallback (`sources/extra.py`).
5. **ai_quantum_breakout theme momentum** — fast + maximally crowded, the exact race we lose
   (prior already confessed at 0.95). → hint retired; the enum stays for stored patterns.
6. **No per-catalyst graduation.** The pooled P(win) verdict (Brier flat at n=16k live AND
   n=2M historical) averaged the 1.0-win-rate families against noise until everything read
   0.25. → graduation ladder in `patterns.py`, wired into `brain.py` (live gate + shadow
   boost + dead-catalyst explore skip) and the daily status (`main.py`).
7. **No small-cap tilt.** Under-covered is where slow diffusion lives. → micro-cap (< $300M)
   setups WITH a trigger family get ×1.06. (The $5M dollar-volume floor and all risk law are
   untouched — safety is not a knob.)

## Measurement — how we know if it's working (honest gauges, no vanity)

- **By ~2026-08-01:** ≥1 of {insider_cluster, gov_contract, demand_surge, index_inclusion}
  shows Wilson-lower > field at n ≥ 30 on FORWARD explore outcomes → the doctrine is right;
  scale that family's explore share.
- Explore dir-hit should leave the 44% floor (crowded-fast churn removed) and per-catalyst
  alpha becomes the scorecard line that matters — not the pooled Brier.
- **If NOTHING graduates by ~2026-08-15** at honest n: the edge is not in the current source
  set. The pivot is DATA (deeper microcap filings coverage), not more learning-loop surgery.
  Say so in the status banner; do not soften it.

## Standing orders for the recursive loop (Hive / Forge / self-code / engineer loop)

1. Propose changes that push a specific catalyst toward graduation — better relative-surprise
   features, better microcap name-resolution, earlier Form-4 cluster detection — before ANY
   pooled-model tuning.
2. Never add a source without stating its diffusion half-life; if it's minutes, it's CONFIRM
   at best.
3. Never count crowd agreement as information. Crowding is a cost.
4. The sim/Forge fitness must stay embargoed + forward-tested (luck floor stands); a champion
   that can't beat forward data gets zero live influence.
5. Rails are not in scope. Ever. `identity.mode`, the ladder, risk law, the Gauntlet and the
   real-money gate are Brady-only.
