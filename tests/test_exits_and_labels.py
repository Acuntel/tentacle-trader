"""Exit geometry and the LABELS the book writes — the autopsy of 2026-09-01, pinned.

Everything here comes from one post-mortem. The book had been losing for mechanical reasons that
nothing in the repo could catch, because nothing in the repo was a test:

  • the profit target had inverted to ~4% under a 6% stop (22 stops at -6.30% = -$173 against
    18 targets at +4.59% = +$76 — breakeven needs a 62% hit rate, the book ran 41%),
  • every gap-through was booked at exactly the stop, so the loss distribution the learners saw
    had its tail cut off,
  • positions entered after-hours were "stopped" against the prior regular close (AFRM, marked at
    exactly -6.40 the minute the extended tape ended),
  • one thin 1-minute extended print stopped LLY at $450,
  • five reverse-split prints summed -6,815 of the -6,713 lifetime explore total,
  • resolved_obs.jsonl crossed the 5MB backup ceiling and the state mirror froze on 07-06.

Commit 4e4b08c ("Phase 1") fixed all of that. These tests exist so it can never silently revert —
including by the bot's own self-coder, which commits to main daily and has flip-flopped a single
constant twelve times.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone

import pytest

from earshot import backup, costs, learning, market, options, prices, technicals
from earshot.brain import Brain
from earshot.exits import ExitEngine
from earshot.learning import Learner
from earshot.main import ClosedMarketExits
from earshot.models import Candidate, Catalyst, RawSignal
from earshot.patterns import PatternMemory

# Tue 2026-09-01 10:00 ET (14:00 UTC, EDT) — a plain regular session. Labor Day 2026 is the 7th.
REG_TS = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc).timestamp()
# Tue 2026-09-01 17:00 ET — after-hours, the session AFRM was entered in.
EXT_TS = datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc).timestamp()
NEXT_DAY_TS = REG_TS + 86400


@pytest.fixture
def clock(monkeypatch):
    """Freeze only the *bare* market.session() call (the "what session is it right now" one) to a
    regular Tuesday, while session(dt) keeps its REAL calendar logic. market.was_closed() therefore
    still genuinely classifies a timestamp, which is exactly what the phantom-stop guard depends on.
    """
    real_session = market.session

    def fake(dt_utc=None):
        return real_session(dt_utc if dt_utc is not None
                            else datetime.fromtimestamp(REG_TS, timezone.utc))

    monkeypatch.setattr(market, "session", fake)
    return market


class _Instinct:
    """Stand-in for MotherInstinct: resolve_due only needs record() -> a dict with 'felt'."""

    def __init__(self):
        self.calls = []

    def record(self, confidence, ret, size_usd, alpha=None):
        self.calls.append({"confidence": confidence, "ret": ret, "size": size_usd, "alpha": alpha})
        return {"felt": -1.0, "note": "test"}


def _learner():
    lr = Learner({"edgar_8k": 1.0}, {"edgar_8k": 1.0})
    lr.lgate = None
    lr.core = None
    return lr


def _pos(**kw):
    p = {"ticker": "ZZZ", "catalyst": "earnings_beat", "confidence": 70.0, "size_usd": 100.0,
         "entry_price": 100.0, "entry_ts": REG_TS, "hold_days": 5, "target_pct": 12.0,
         "stop_pct": 6.0, "runup_pct": None, "sources": ["edgar_8k"], "pattern": ""}
    p.update(kw)
    return p


def _outcome_rows():
    with open(learning.OUTCOMES_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def _learn_rows():
    with open(learning.LEARN_OUT_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def _brain(cfg, know):
    """A Brain wired for hermetic evaluation: no price scans, no LLM, no prediction markets."""
    for section in ("technicals", "options", "fundamentals"):
        cfg.setdefault(section, {})["enabled"] = False
    cfg.setdefault("fda_odds", {})["enabled"] = False
    b = Brain(cfg)
    b.patterns = PatternMemory(know)
    return b


def _signals(hint):
    """Two independent sources from two different families (filing + surprise) on one ticker."""
    return [RawSignal(source="edgar_8k", weight=1.0, ts=REG_TS, tickers=["ZZZ"],
                      headline="filing", catalyst_hint=hint),
            RawSignal(source="finnhub_earnings", weight=1.0, ts=REG_TS, tickers=["ZZZ"],
                      headline="surprise", catalyst_hint=hint)]


# --------------------------------------------------------------------------------------------
# 1. REWARD : RISK
# --------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_no_candidate_may_carry_a_target_below_min_reward_risk_times_its_stop(cfg, no_prices):
    """Every Candidate Brain.evaluate builds carries target_pct >= min_reward_risk x stop_pct, for
    EVERY catalyst in config.yaml, even when the learned pattern payoff is tiny.

    Autopsy 2026-09-01: the book ran a ~4% target under a 6% stop. That geometry needs a 62% hit
    rate just to break even (22 stops at -6.30% = -$173 vs 18 targets at +4.59% = +$76); the book
    hit 41%. brain.py:238-239 now floors the target at conviction.min_reward_risk x the stop.
    """
    min_rr = float(cfg["conviction"]["min_reward_risk"])
    assert min_rr >= 1.5, "min_reward_risk was weakened below the autopsy's 1.5 floor"

    know = {}
    brain = _brain(cfg, know)
    checked = []
    for name, ccfg in cfg["catalysts"].items():
        sig = brain.patterns.signature(name, {"filing", "surprise"}, "")
        for _ in range(8):                      # teach it a 1.0%-per-win payoff: as small as it gets
            brain.patterns.record(sig, True, 1.0)
        assert brain.patterns.suggested_target(sig, ccfg["target_pct"]) <= 1.0

        cands = brain.evaluate(_signals(name), 500.0, False)
        assert len(cands) == 1
        c = cands[0]
        assert c.target_pct >= min_rr * c.stop_pct, (
            f"{name}: target {c.target_pct}% under a {c.stop_pct}% stop is a losing geometry")
        checked.append(name)
    assert set(checked) == set(cfg["catalysts"]), "a catalyst escaped the reward:risk check"


@pytest.mark.invariant
def test_the_reward_risk_floor_is_what_raises_the_target_not_the_config_default(cfg, no_prices):
    """The floor is load-bearing: with min_reward_risk removed, the very same learned pattern drops
    the target to 1.5% under a 6% stop — the exact inversion the autopsy found. With it, 9.0%.

    Pins brain.py:238-239 against a self-coder edit that deletes or zeroes the knob.
    """
    know = {}
    brain = _brain(cfg, know)
    sig = brain.patterns.signature("index_inclusion", {"filing", "surprise"}, "")
    for _ in range(8):
        brain.patterns.record(sig, True, 1.5)

    floored = brain.evaluate(_signals("index_inclusion"), 500.0, False)[0]
    assert floored.stop_pct == 6
    assert floored.target_pct == pytest.approx(9.0), "1.5 x a 6% stop must be 9%"

    cfg["conviction"]["min_reward_risk"] = 0.0          # simulate the regression
    unfloored = _brain(cfg, know)
    unfloored.patterns = brain.patterns
    naked = unfloored.evaluate(_signals("index_inclusion"), 500.0, False)[0]
    assert naked.target_pct == pytest.approx(1.5), (
        "without the floor the learned payoff sets the target — this is the losing geometry")


# --------------------------------------------------------------------------------------------
# 2. suggested_target is a CEILING, never a floor
# --------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_suggested_target_is_a_ceiling_on_the_config_default_in_both_directions(cfg):
    """patterns.suggested_target may only SHRINK the config target, never raise it, and it no
    longer floors at 4.0.

    Autopsy 2026-09-01: the old body was ``max(4.0, ...)``, which stamped a ~4% target onto every
    book trade regardless of the catalyst's 6-12% config target (22 stops -$173 vs 18 targets
    +$76). patterns.py:191-201 is now ``min(default, ...)``.
    """
    pm = PatternMemory({})

    rich = pm.signature("mna_target", {"filing", "surprise"}, "")
    for _ in range(8):
        pm.record(rich, True, 20.0)              # a fat learned payoff
    assert pm.suggested_target(rich, 10.0) == 10.0, "a big learned payoff must not raise the target"
    assert pm.suggested_target(rich, 3.0) == 3.0, "a small config default must be respected as-is"

    thin = pm.signature("gov_contract", {"filing", "surprise"}, "")
    for _ in range(8):
        pm.record(thin, True, 1.5)               # a thin learned payoff
    assert pm.suggested_target(thin, 10.0) == 1.5, "a small learned payoff must shrink the target"
    assert pm.suggested_target(thin, 10.0) < 4.0, "the old max(4.0, ...) floor is back"

    cold = pm.signature("activist_stake", {"filing", "surprise"}, "")
    assert pm.suggested_target(cold, 15.0) == 15.0, "a data-poor pattern must not move the target"


# --------------------------------------------------------------------------------------------
# 3. resolve_due records the REAL move
# --------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_resolve_due_books_the_real_gap_not_the_stop(cfg, no_prices, no_costs, clock, monkeypatch):
    """A -15% gap through a 6% stop is booked at -15%, and the row carries raw_pct, clamped_pct,
    mfe_pct, mae_pct, entry_session and exit_session.

    Autopsy 2026-09-01: the old clamp wrote every gap-through at exactly the stop, so the ledger,
    mastery, patterns and the P(win) model all learned a loss distribution with the tail cut off
    (learning.py:130-134). clamped_pct is kept for reference only; pnl_pct is now the real move.
    """
    monkeypatch.setattr(prices, "last_price", lambda t, extended=False: 85.0)
    lr = _learner()
    lr.open = [_pos(peak=110.0, trough=97.0)]

    resolved = lr.resolve_due(_Instinct(), ExitEngine(cfg))
    assert [r["exit"] for r in resolved] == ["stop"]

    row = _outcome_rows()[-1]
    assert row["pnl_pct"] == pytest.approx(-15.0), "the gap was clamped back to the stop"
    assert row["raw_pct"] == pytest.approx(-15.0)
    assert row["clamped_pct"] == pytest.approx(-6.0), "clamped_pct must still be recorded, separately"
    assert row["mfe_pct"] == pytest.approx(10.0)      # peak 110 vs entry 100
    assert row["mae_pct"] == pytest.approx(-15.0)     # trough ratcheted down to the 85 mark
    assert row["entry_session"] == "regular"
    assert row["exit_session"] == "regular"
    assert row["exit"] == "stop" and row["win"] is False
    assert row["pnl_usd"] == pytest.approx(-15.0)     # $100 size x -15%
    assert lr.open == [], "the resolved position must leave the open book"


@pytest.mark.invariant
def test_resolve_due_exit_price_is_the_live_mark_not_a_back_solved_price(
        cfg, no_prices, clock, monkeypatch):
    """exit_price is the price actually read, not entry x (1 + return/100) recomputed after the
    cost debit.

    Autopsy 2026-09-01: back-computing the exit from the clamped return fabricated a price that
    never traded (learning.py:141). With a 0.4% round trip the back-solve would print 84.60 on a
    real 85.00 mark.
    """
    monkeypatch.setattr(prices, "last_price", lambda t, extended=False: 85.0)
    monkeypatch.setattr(costs, "round_trip_pct", lambda t: 0.4)
    lr = _learner()
    lr.open = [_pos()]

    lr.resolve_due(_Instinct(), ExitEngine(cfg))
    row = _outcome_rows()[-1]

    assert row["exit_price"] == pytest.approx(85.0), "the ledger must carry the real mark"
    back_solved = round(row["entry_price"] * (1 + row["pnl_pct"] / 100), 4)
    assert row["exit_price"] != pytest.approx(back_solved)
    assert back_solved == pytest.approx(84.6)


# --------------------------------------------------------------------------------------------
# 4. PHANTOM-STOP GUARD
# --------------------------------------------------------------------------------------------

@pytest.mark.rail
@pytest.mark.invariant
def test_closed_market_never_marks_a_position_entered_off_hours(
        cfg, no_prices, no_costs, clock, monkeypatch):
    """With the market fully shut, a position whose entry_ts was in an extended session is not
    resolved at all — not even at a stop price. It waits for real tape.

    Autopsy 2026-09-01: with the market closed the price source reverts to the last REGULAR close,
    which for an after-hours entry PREDATES the entry. AFRM was entered after-hours on earnings and
    "stopped" at exactly -6.40 the minute the extended tape ended (learning.py:96-101).
    """
    monkeypatch.setattr(prices, "last_price", lambda t, extended=False: 85.0)
    lr = _learner()
    lr.open = [_pos(ticker="AFRM", entry_ts=EXT_TS)]

    resolved = lr.resolve_due(_Instinct(), ExitEngine(cfg), closed=True)

    assert resolved == [], "an off-hours entry was marked against a stale regular close"
    assert len(lr.open) == 1 and lr.open[0]["ticker"] == "AFRM"
    assert not os.path.exists(learning.OUTCOMES_PATH), "a phantom stop reached the immutable ledger"


@pytest.mark.invariant
def test_closed_market_still_resolves_a_regular_session_entry(
        cfg, no_prices, no_costs, clock, monkeypatch):
    """The guard is narrow: a position entered DURING the regular session still resolves when the
    market is closed, so the fix cannot be a blanket "never resolve off-hours" freeze.

    Same fix as above (learning.py:96-101) — the discriminator is market.was_closed(entry_ts).
    """
    monkeypatch.setattr(prices, "last_price", lambda t, extended=False: 85.0)
    lr = _learner()
    lr.open = [_pos(ticker="REGL", entry_ts=REG_TS)]

    resolved = lr.resolve_due(_Instinct(), ExitEngine(cfg), closed=True)

    assert [r["exit"] for r in resolved] == ["stop"]
    assert lr.open == []
    assert _outcome_rows()[-1]["entry_session"] == "regular"


# --------------------------------------------------------------------------------------------
# 5. EXTENDED STOP needs two consecutive reads
# --------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_extended_stop_requires_two_consecutive_through_stop_reads(cfg):
    """On the thin extended tape the price must sit THROUGH the stop on two consecutive reads.
    In the regular session the stop is immediate.

    Autopsy 2026-09-01: LLY was stopped at $450 by one after-hours 1-minute print (exits.py:51-59).
    The confirmation counter resets the moment price recovers, so two non-adjacent bad prints do
    not add up to a stop.
    """
    ex = ExitEngine(cfg)
    pos = _pos(ticker="LLY")

    first, chg = ex.evaluate(pos, 92.0, extended=True)
    assert first is None, "a single extended print fired the stop"
    assert chg == pytest.approx(-8.0), "the return is still reported on the suppressed read"

    second, _ = ex.evaluate(pos, 92.0, extended=True)
    assert second == "stop", "a confirmed extended stop must fire on the second read"

    # ...and the counter is a CONSECUTIVE counter, not a lifetime tally.
    pos2 = _pos(ticker="LLY")
    assert ex.evaluate(pos2, 92.0, extended=True)[0] is None
    assert ex.evaluate(pos2, 101.0, extended=True)[0] is None      # recovered -> counter resets
    assert ex.evaluate(pos2, 92.0, extended=True)[0] is None, "the reset was skipped"

    # Regular session: no confirmation delay, the stop is protective and immediate.
    assert ExitEngine(cfg).evaluate(_pos(ticker="LLY"), 92.0, extended=False)[0] == "stop"


# --------------------------------------------------------------------------------------------
# 6. ClosedMarketExits
# --------------------------------------------------------------------------------------------

@pytest.mark.rail
def test_closed_market_exits_lets_only_the_stop_survive(cfg):
    """Off-hours the only exit that may fire on the REAL book is the hard stop. Target, trailing
    and time all resolve at the next open, where the price actually carries the news.

    main.py:52-64 — a discretionary exit against a frozen prior close would book an untradeable
    price into the immutable ledger AND mastery. The return is still reported in every case.
    """
    shim = ClosedMarketExits(ExitEngine(cfg))

    assert shim.evaluate(_pos(), 92.0)[0] == "stop", "the protective stop must stay armed off-hours"

    reason, chg = shim.evaluate(_pos(), 115.0)                     # +15% through a 12% target
    assert reason is None, "a target filled at a frozen close"
    assert chg == pytest.approx(15.0), "the return must still be reported"

    # time exit: held past hold_days at a flat price
    old = _pos(entry_ts=REG_TS - 30 * 86400, hold_days=5)
    assert ExitEngine(cfg).evaluate(old, 100.0, now=REG_TS)[0] == "time"
    assert shim.evaluate(_pos(entry_ts=REG_TS - 30 * 86400, hold_days=5),
                         100.0, now=REG_TS)[0] is None

    # trailing exit: +10% peak then a give-back past trail_pct
    trail = _pos(peak=110.0)
    assert ExitEngine(cfg).evaluate(trail, 101.0)[0] == "trailing"
    assert shim.evaluate(_pos(peak=110.0), 101.0)[0] is None


@pytest.mark.invariant
def test_closed_market_exits_passes_extended_through_to_the_engine(cfg):
    """The shim forwards `extended` rather than swallowing it, so the two-read extended-stop
    confirmation still applies through it (main.py:62-64 -> exits.py:55-59).
    """
    shim = ClosedMarketExits(ExitEngine(cfg))
    pos = _pos()

    assert shim.evaluate(pos, 92.0, extended=True)[0] is None, "extended was not forwarded"
    assert pos["ext_stop_hits"] == 1, "the engine never saw extended=True"
    assert shim.evaluate(pos, 92.0, extended=True)[0] == "stop"


# --------------------------------------------------------------------------------------------
# 7-8. CORRUPT-PRINT GUARD and the explore window
# --------------------------------------------------------------------------------------------

def _open_learning(lr, ticker, price, direction=1):
    c = Candidate(ticker=ticker, catalyst=Catalyst.EARNINGS_BEAT, confidence=70.0, size_usd=0.0)
    c.stop_pct, c.target_pct, c.hold_days = 8.0, 12.0, 5
    assert lr.open_learning(c, price, direction, 100.0)


@pytest.mark.rail
@pytest.mark.invariant
def test_corrupt_print_is_ledgered_as_corrupt_and_credits_nothing(no_prices, no_costs, monkeypatch):
    """A |move| > 60% is logged with exit "corrupt" and alpha 0.0 and increments NO explore
    counter, while a real -8% stop in the same pass is credited normally.

    Autopsy 2026-09-01: five reverse-split prints (LMFA -2415%, PRPL -2085%, GNPX -1872%, ...)
    summed -6,815 of the ledger's -6,713 lifetime total and drove the "no edge" headline, the
    explore-slot starvation and the catalyst_dead calls (learning.py:207-219).
    """
    lr = _learner()
    _open_learning(lr, "LMFA", 10.0)          # reverse-split glitch print
    _open_learning(lr, "REAL", 10.0)          # an honest loser
    monkeypatch.setattr(prices, "last_price", lambda t, **k: {"LMFA": 40.0, "REAL": 9.2}[t])

    res = lr.resolve_learning()

    rows = {r["ticker"]: r for r in _learn_rows()}
    assert rows["LMFA"]["exit"] == "corrupt"
    assert rows["LMFA"]["alpha"] == 0.0, "a glitch print credited alpha"
    assert rows["LMFA"]["cost"] == 0.0 and rows["LMFA"]["long_win"] is None
    assert rows["LMFA"]["ret"] == pytest.approx(300.0), "the raw glitch must still be recorded"

    assert rows["REAL"]["exit"] == "stop", "the guard swallowed a legitimate outcome"
    assert res["resolved"] == 1, "the corrupt row must not count as a resolution"

    ex = lr.know["explore"]
    assert ex["trades"] == 1, "the corrupt print incremented the explore trade counter"
    assert ex["sum_alpha"] == pytest.approx(rows["REAL"]["alpha"])
    assert ex["sum_alpha"] > -60.0, "a reverse-split print reached the explore accumulator"
    assert "LMFA" not in lr.know.get("learn_seen", {}), "a corrupt row armed the reopen guard"
    assert [p["ticker"] for p in lr.learn_open] == [], "both learning trades should be closed out"


@pytest.mark.invariant
def test_resolve_learning_appends_a_bounded_explore_recent_window(no_prices, no_costs, monkeypatch):
    """Each resolved learning trade appends {ts, dir, alpha, exit} to know["explore_recent"], and
    the window stays bounded at 600 rows so the clean practice gauge (core.velocity) never reads an
    unbounded, ever-older tail (learning.py:257-260).
    """
    lr = _learner()
    lr.know["explore_recent"] = [{"ts": i, "dir": 1, "alpha": 0.0, "exit": "time"}
                                 for i in range(700)]
    _open_learning(lr, "REAL", 10.0)
    monkeypatch.setattr(prices, "last_price", lambda t, **k: 9.2)

    lr.resolve_learning()

    rec = lr.know["explore_recent"]
    assert len(rec) == 600, "the explore_recent window is unbounded"
    last = rec[-1]
    assert set(last) == {"ts", "dir", "alpha", "exit"}
    assert last["dir"] == 1 and last["exit"] == "stop"
    assert last["alpha"] == pytest.approx(-8.0)      # -8% move, zero market return, zero costs
    assert last["ts"] > 0


# --------------------------------------------------------------------------------------------
# 9. LEDGER ROTATION
# --------------------------------------------------------------------------------------------

@pytest.mark.rail
@pytest.mark.invariant
def test_append_rotates_a_ledger_that_outgrew_the_backup_ceiling():
    """learning._append rolls a ledger past ROTATE_BYTES to a dated sibling and starts a fresh
    file, and ROTATE_BYTES sits under the 5MB the off-volume backup will actually upload.

    Autopsy 2026-09-01: resolved_obs.jsonl crossed 5MB in July, backup.py skips anything that big
    (backup.MAX_FILE), and the state mirror plus the CI fitness gate scored against a ledger frozen
    on 07-06 ever since (learning.py:32-42).
    """
    assert learning.ROTATE_BYTES < backup.MAX_FILE, (
        "the rotation threshold no longer protects the backup ceiling")

    path = "state/resolved_obs.jsonl"
    os.makedirs("state", exist_ok=True)
    with open(path, "w") as f:
        f.write("x" * (backup.MAX_FILE + 100_000))          # bigger than backup will ever upload
    assert os.path.getsize(path) > backup.MAX_FILE

    learning._append(path, {"row": 1})

    siblings = [p for p in glob.glob(path + ".*")]
    assert len(siblings) == 1, "the oversized ledger was not rolled to a dated sibling"
    assert os.path.getsize(siblings[0]) > backup.MAX_FILE
    with open(path) as f:
        live = [json.loads(line) for line in f if line.strip()]
    assert live == [{"row": 1}], "the fresh ledger must start clean"
    assert os.path.getsize(path) < learning.ROTATE_BYTES

    learning._append(path, {"row": 2})                      # a small file must NOT rotate
    assert len(glob.glob(path + ".*")) == 1
    with open(path) as f:
        assert len([line for line in f if line.strip()]) == 2


# --------------------------------------------------------------------------------------------
# 10. DRIFT DISCIPLINE
# --------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_drift_discipline_spares_classified_catalysts_from_reactive_exits(cfg, monkeypatch):
    """With drift_discipline on, a CLASSIFIED catalyst exits only on stop/target/trailing/time; the
    `unknown` discovery pool keeps every protective reactive exit.

    Ledger 2026-07-25: reactive exits chopped the very drift the entries hunt — 32/70 conviction
    closes were `technical` at -0.04% avg and only 2/70 ever reached target, while explore trades
    (stop/target/time only) hit target on 16/45 fda_approval setups at +6.2% (exits.py:32-38, 72).
    """
    monkeypatch.setattr(technicals, "exit_read", lambda t, c: (True, "technical"))
    monkeypatch.setattr(options, "exit_read", lambda t, c: (True, "options_bearish"))
    assert cfg["exits"]["drift_discipline"] is True, "drift_discipline was turned off in config"
    ex = ExitEngine(cfg)

    classified = _pos(catalyst="fda_approval", hold_days=30)
    assert ex.evaluate(classified, 100.0, now=NEXT_DAY_TS)[0] is None, (
        "a classified catalyst took a reactive exit mid-drift")

    unknown = _pos(catalyst="unknown", hold_days=30)
    assert ex.evaluate(unknown, 100.0, now=NEXT_DAY_TS)[0] == "technical", (
        "the discovery pool lost its protective reactive exits")

    # Discipline is not a suspension of protection: the hard stop still fires on a classified name.
    assert ex.evaluate(_pos(catalyst="fda_approval", hold_days=30), 90.0, now=NEXT_DAY_TS)[0] == "stop"


# --------------------------------------------------------------------------------------------
# 11. COST DEBIT
# --------------------------------------------------------------------------------------------

@pytest.mark.rail
@pytest.mark.invariant
def test_label_is_net_of_costs_and_an_extended_entry_pays_the_friction_premium(
        cfg, no_prices, clock, monkeypatch):
    """Every label is debited the liquidity-tiered round trip BEFORE anything learns from it, and a
    position entered off-hours pays 2.5x that friction — real extended spreads run several times
    wider (learning.py:118-124, costs.py:49).
    """
    monkeypatch.setattr(prices, "last_price", lambda t, extended=False: 90.0)
    monkeypatch.setattr(costs, "round_trip_pct", lambda t: 0.4)
    lr = _learner()
    lr.open = [_pos(ticker="DAY", entry_ts=REG_TS),
               _pos(ticker="NGT", entry_ts=EXT_TS)]

    lr.resolve_due(_Instinct(), ExitEngine(cfg))

    rows = {r["ticker"]: r for r in _outcome_rows()}
    assert rows["DAY"]["cost_pct"] == pytest.approx(0.4)
    assert rows["DAY"]["raw_pct"] == pytest.approx(-10.0)
    assert rows["DAY"]["pnl_pct"] == pytest.approx(-10.4), "the label was not debited the round trip"

    assert rows["NGT"]["entry_session"] == "afterhours"
    assert rows["NGT"]["cost_pct"] == pytest.approx(1.0), "the 2.5x extended premium is gone"
    assert rows["NGT"]["pnl_pct"] == pytest.approx(-11.0)
    assert rows["NGT"]["cost_pct"] > rows["DAY"]["cost_pct"]

    # and the debit rides into what the learners see: alpha is net, not gross
    assert rows["NGT"]["alpha"] == pytest.approx(-11.0)


@pytest.mark.rail
def test_costs_are_configured_not_learnable(raw_config):
    """The friction model is config, not a learned parameter: an edge that only exists gross of
    costs must never graduate. Pins costs.enabled and the tier table shipping non-zero.
    """
    c = raw_config["costs"]
    assert c["enabled"] is True, "cost debiting was switched off"
    assert float(c["default_side_pct"]) > 0
    tiers = c["tiers"]
    assert tiers == sorted(tiers, key=lambda t: -t[0]), "tiers must be ordered by descending volume"
    assert all(float(pct) > 0 for _, pct in tiers), "a zero-cost tier makes shadow fills free again"
