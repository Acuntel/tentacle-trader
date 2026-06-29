# TENTACLE — VELOCITY

### A master prompt for turning the recursive loops into genuinely fast, continuous self-improvement

---

## MANDATE

Stop making the machine *busier*. Make it *learn the maximum true thing per hour.*

You are **not** adding loops, models, tentacles, or strategies. You are re-pointing the compute that already exists at the only quantity that produces real improvement: **the rate of fresh, out-of-sample ground truth converted into honest belief updates.** Everything below serves that and nothing else.

---

## THE ONE LAW (every change must raise this number)

```
learning velocity  =  (fresh forward samples / day)
                    ×  (information per sample)
                    ×  (fidelity of each belief update)
                    −  (overfitting that fakes improvement)
```

Maximize the first three terms. Drive the fourth to zero. If a change doesn't increase learning velocity, revert it — no matter how clever it is.

---

## THE DIAGNOSIS YOU ARE FIXING (measured, not guessed)

- **19** lifetime forward trades. mastery **0.0/60**. P(win) model **brier 0.251** = the no-skill baseline.
- coach verdict: **OVERFIT — no out-of-sample survivor of 200 strategies.**
- sims replay the same 10y history **311,000×**; `sim-son` equity = **$3.7×10⁶⁰** = pure overfit to a frozen path.
- The machinery is healthy. It learns nothing because: **(a)** real samples are starved, **(b)** the "infinite" loops churn frozen history, **(c)** updates can't outrun the noise.

The fix is not more activity. It is more *truth per hour.*

---

## PRIME DIRECTIVE — decouple LEARNING from CONVICTION

In **shadow only** (the money gate is never touched):

- A **conviction trade** exists to make money. It stays rare, gated, high-bar.
- A **learning trade** exists to *buy information.* In shadow, information is **free** — so buy enormous amounts of it.

These have different economics and must be different code paths. Today the bot fires ~1 paper trade a day because it gates learning behind conviction. That starves every downstream learner. Separate them.

---

## WORKSTREAM 1 — FLOOD THE LOOP WITH FRESH GROUND TRUTH  *(the #1 lever — do first)*
Files: `brain.py`, `learning.py`, `config.yaml`, `exits.py`

- Add a **shadow exploration budget**: every cycle, *in addition to* any conviction trade, open paper positions on the top-K **most-uncertain** candidates — **ignoring the conviction bar, ignoring the downtrend long-suppression, both long and short.**
  - Short side: add a tiny inverse-ETF map (SQQQ/SPXU/SOXS) **or** just compute synthetic short PnL — shadow doesn't need a real instrument.
- Tag these `learning_trade = True`. They feed patterns / P(win) / source-weights / calibration, but **NEVER** the mastery ladder or the money gate (those stay earned on conviction-grade forward trades only — anti-self-deception).
- Keep multi-horizon resolution (1h / 24h / 120h) but set each **catalyst's horizon to its real drift window** (earnings ≠ M&A ≠ activist ≠ contract award).
- **Target: n goes 19 → thousands in two weeks.** Every downstream learner is currently data-starved; this is what unblocks all of them.

## WORKSTREAM 2 — MAKE EVERY SAMPLE TEACH THE MOST  *(active learning)*
Files: `brain.py` (candidate sort), `learnedgate.py`

- Spend the exploration budget on **maximum-uncertainty** setups first: P(win) nearest 0.5, or largest disagreement between the LLM analyst and the learned model. Certain setups teach nothing; uncertain ones carry the most information.
- Enforce **sample diversity**: cap exploration per-catalyst and per-sector so no single pattern hogs the budget — every pattern reaches significance, not just the loud ones.
- Keep **counterfactual labeling** (observations on setups you didn't fire), down-weighted vs real paper fills.

## WORKSTREAM 3 — HONEST, FAST BELIEF UPDATES
Files: `learnedgate.py`, `learning.py`, `strategies.py`, `core.py`

- **Adaptive learning rate**: high LR while a pattern/source has few samples (learn fast when ignorant), decaying as n grows (stop thrashing). Per-pattern and per-source.
- **Historical search may only PROPOSE; forward outcomes alone may CONFIRM.** Every live-param change must clear walk-forward K-fold + the multiple-testing luck-floor on **forward** data before it changes behavior.
- Keep **drift detection** (recent precision < lifetime − 0.2 → fast cut); make it per-pattern and per-regime.
- **Calibration is a first-class loop:** continuously refit stated→realized. A well-calibrated 0.5 is honest and useful; a miscalibrated 0.7 is dangerous.

## WORKSTREAM 4 — KILL THE LOOPS THAT FAKE LEARNING  *(reclaim the compute)*
Files: `sim.py`, `strategies.py`, `forge.py`, `coach.py`

- **Stop replaying frozen history unboundedly.** Cap sim replays per unique data slice; once a slice's marginal information ≈ 0, stop and free the workers. Redirect that CPU to Workstream 1's forward paper trades.
- **Reset and cap `sim-son` equity** (reset per episode-batch). The $10⁶⁰ number is the alarm: a sim that *cannot lose* teaches nothing.
- **The 200-strategy tournament becomes a HYPOTHESIS GENERATOR only.** Its winners are *candidate ideas* that must earn their keep on **forward** paper trades before touching any live param. A tournament winner never directly changes live behavior. (This is exactly why the luck-floor keeps rejecting them — respect it; don't loosen it.)
- **Forge: same rule.** Free search proposes; forward data disposes.

## WORKSTREAM 5 — SHRINK OODA LATENCY
Files: `learning.py`, `main.py`, `llm.py`

- Minimize wall-clock from **outcome → belief → next decision.** Resolved outcomes must update the live model **within the same cycle**, not in a daily batch.
- **Keep the LLM out of the per-tick hot path.** LLMs are the *rare oracle* (hypothesis generation, hard adjudication, code proposals). The fast loop must be pure-CPU deterministic so it can run thousands of times for free. An LLM inside the gate is a latency and cost anchor.

## WORKSTREAM 6 — MAKE "LEARNING SPEED" THE HEADLINE METRIC  *(so you can SEE it work)*
Files: `survival.py` (dashboard), `coach.py` (daily heartbeat), `core.py`

Track these as **trajectories, not points**, on the dashboard and the daily Telegram heartbeat:

- **forward samples / day** — is the flood working?
- **brier-score trend** — is calibration actually improving? *(flat after thousands of samples → it is NOT learning; say so loudly.)*
- **OOS alpha/trade vs SPY, trend** — is any edge emerging?
- **time-to-detect a regime shift** — is it adapting faster?
- **information half-life** — how fast a new pattern reaches significance.

If these trend up, it is genuinely learning faster. If brier stays flat, the system must **announce "no edge detected — configuration likely has none"** — quickly and honestly.

---

## THE RAILS (LOCKED — these are what make speed REAL, not fake)

SPY-alpha labels · walk-forward K-fold · multiple-testing luck-floor · **forward-only mastery** · the money gate · hard risk caps.

Faster learning *without* these is just faster overfitting. **Never trade a rail for speed.** The self-coder still cannot touch `risk` / `executor` / `backtest` / `selfcode` / `apply_patch`, nor introduce any money-unlock token.

---

## THE BRUTAL-TRUTH CLAUSE (re-read every cycle)

Fast learning **does not manufacture an edge.** If the edge isn't in free, public, multi-minute-latency data on a $500 long-ish equity book, learning faster just lets you **prove that in 2 weeks instead of 6 months** — and that is a *win*, because it tells you to change the data or the market, not to keep polishing loops.

The goal of VELOCITY is **fast truth**: find the edge quickly, or disprove it quickly. A system that reaches a confident, well-calibrated *"no edge here"* has **succeeded at learning.** Do not let activity masquerade as progress — the Workstream 6 metrics are the only judge.

---

## BUILD ORDER

1. **WS1** — flood forward samples *(biggest lever)*
2. **WS6** — learning-speed metrics *(so WS1 is measurable)*
3. **WS4** — kill the fake loops, reclaim compute
4. **WS2 + WS3** — squeeze info/sample, honest updates
5. **WS5** — latency

Ship each behind the existing test-gate. **>5 files in one step → stop and outline first.** Shadow / $0 throughout. Deploy via `railway up` from the repo dir; **`git fetch && git rebase origin/main` first** — the autonomous self-evolve loop commits to main on its own.
