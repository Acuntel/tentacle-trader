"""Honesty gauges + NaN/persistence hygiene — the autopsy of 2026-09-01, pinned.

Two families of finding live in this file.

THE GAUGES. The "is it on the right path?" banner printed ON TRACK off a +1 mastery move while the
realized shadow return was -7.7%/month, and the practice-alpha headline (-2.01%/trade) was driven by
five corrupt reverse-split SHORT prints on a long-only account. The fix made the verdict key on two
gauges that can only move on real money and clean, recent, long-only practice — and mastery is not
one of them. These tests hold that door shut.

THE NaN ROT. A single non-finite float, once written into knowledge.json / sim.json, propagates
forever: NaN != NaN, so every mean, every accumulator and every threshold computed from it comes back
NaN silently. Live boot healed 50 non-finite values in knowledge.json and 6 in sim.json; six strategy
folds carried a NaN `u`, which made the multiple-testing luck floor NaN and disabled the overfit
rejection entirely. These tests pin every place the fix pass installed a guard.

Everything here is hermetic: no network, no sleeps, no LLM, and (via the autouse ``isolated_state``
fixture) no real state directory.
"""
from __future__ import annotations

import json
import math
import os
import time

import pytest

from earshot import history, marketadj, persist
from earshot.core import BrainCore
from earshot.scout import SourceScout
from earshot.sim import SimEngine
from earshot.strategies import StrategyBook
from earshot.survival import MotherInstinct

NAN = float("nan")
INF = float("inf")


# --------------------------------------------------------------------------------------------
# helpers — fakes only for the collaborators velocity() reads through, never for logic under test
# --------------------------------------------------------------------------------------------
class FakeGate:
    """The learned gate as velocity() sees it: a sample count, a Brier score, a trust scalar."""

    def __init__(self, n=0, brier=0.25, trust=0.0):
        self.n, self.brier, self._trust = n, brier, trust

    def trust(self):
        return self._trust


class FakeInstinct:
    """MotherInstinct as velocity() sees it: one honest realized number, or None."""

    def __init__(self, monthly):
        self._m = monthly

    def monthly_return_pct(self):
        return self._m


def _explore_rows(alphas, *, direction=1, age_days=0.0):
    now = time.time()
    return [{"alpha": float(a), "dir": direction, "ts": now - age_days * 86400.0} for a in alphas]


def _core(cfg, **know):
    """A BrainCore over a hand-built knowledge dict (BrainCore fills the rest of the sections)."""
    k = dict(know)
    k.setdefault("growth", {})
    return BrainCore(k, {}, cfg)


def _verdict(cfg, *, alphas=(), monthly=None, history_days=0, explore=None, extra_rows=()):
    know = {"explore_recent": list(_explore_rows(alphas)) + list(extra_rows)}
    if explore:
        know["explore"] = explore
    if history_days:
        know["growth"] = {"history": [{"day": f"2026-08-{d:02d}", "mastery": float(d)}
                                      for d in range(1, history_days + 1)]}
    core = _core(cfg, **know)
    return core.velocity(FakeGate(n=100, brier=0.24), FakeInstinct(monthly))


# --------------------------------------------------------------------------------------------
# 1-2. the verdict: two honest gauges, and mastery is not one of them
# --------------------------------------------------------------------------------------------
@pytest.mark.invariant
@pytest.mark.parametrize("monthly,alpha,expected", [
    (5.0, 1.0, "ON TRACK"),      # money up AND clean practice up -> and ONLY then
    (5.0, -1.0, "SETTLING"),     # money up, practice losing
    (-7.7, 1.0, "SETTLING"),     # practice up, money losing (the live case)
    (-7.7, -1.0, "SPINNING"),    # both dead
    (None, 1.0, "SETTLING"),     # no realized number yet is NOT a passing money gauge
    (None, -1.0, "SPINNING"),
    (0.0, 1.0, "SETTLING"),      # flat is not positive
    (0.0, 0.0, "SPINNING"),      # flat practice is not positive either
])
def test_verdict_needs_both_money_and_clean_practice(cfg, monthly, alpha, expected):
    """ON TRACK requires BOTH honest gauges positive: realized monthly money > 0 AND clean practice
    alpha > 0. Exactly one positive is SETTLING; neither is SPINNING.

    Autopsy evidence: the banner read ON TRACK while realized shadow return was -7.7%/month, because
    the old verdict fired on a rising mastery delta alone.
    """
    v = _verdict(cfg, alphas=[alpha] * 20, monthly=monthly)
    assert v["clean_n"] == 20, "the 20-row clean sample must actually be in the window"
    assert v["clean_alpha"] == pytest.approx(alpha)
    assert v["monthly_return"] == monthly
    assert v["verdict"] == expected


@pytest.mark.invariant
def test_rising_mastery_alone_can_never_say_on_track(cfg):
    """A climbing mastery score, with NO money and NO clean practice evidence, must NOT produce
    ON TRACK. This is the exact regression that fooled the operator: mastery moved +1, the banner
    said ON TRACK, and realized return was -7.7%/month.

    Here mastery climbs +7 over 8 daily snapshots, realized money is -7.7%/month and there is not a
    single clean practice trade. The only honest answer is SPINNING.
    """
    v = _verdict(cfg, monthly=-7.7, history_days=8)
    assert v["mastery_7d"] is not None and v["mastery_7d"] > 0, "mastery really is rising"
    assert v["mastery_now"] == 8.0
    assert v["clean_alpha"] is None and v["clean_n"] == 0, "no clean practice evidence at all"
    assert v["verdict"] == "SPINNING"
    assert v["verdict"] != "ON TRACK"


# --------------------------------------------------------------------------------------------
# 3. the clean practice gauge: long-only, windowed, trimmed
# --------------------------------------------------------------------------------------------
@pytest.mark.invariant
def test_clean_gauge_is_long_only_and_ignores_the_poisoned_lifetime_accumulator(cfg):
    """The clean practice gauge counts LONG rows only. The lifetime explore accumulator was five
    corrupt reverse-split SHORT prints (a side the long-only cash account can never take) summing
    -6,815 alpha, which is the entire -2.01%/trade headline. The clean gauge must not see them.
    """
    shorts = _explore_rows([-1343.0] * 5, direction=-1)
    v = _verdict(cfg, alphas=[1.0] * 20, monthly=5.0, extra_rows=shorts,
                 explore={"trades": 3390, "sum_alpha": -6815.0, "dir_hits": 1400})
    assert v["avg_explore_alpha"] == pytest.approx(-2.01), "the poisoned lifetime headline, verbatim"
    assert v["clean_n"] == 20, "the 5 short rows are excluded, not merely down-weighted"
    assert v["clean_alpha"] == pytest.approx(1.0)
    assert v["verdict"] == "ON TRACK", "the clean gauge, not the corrupt one, drives the verdict"


@pytest.mark.invariant
def test_clean_gauge_is_windowed_to_velocity_window_days(cfg):
    """Practice rows older than ``velocity.window_days`` are excluded, so a good stretch from months
    ago cannot keep the banner green. Below 20 in-window rows the gauge honestly reports None rather
    than guessing.
    """
    wdays = float(cfg["velocity"]["window_days"])
    stale = _explore_rows([5.0] * 15, age_days=wdays + 5)
    v = _verdict(cfg, alphas=[1.0] * 10, monthly=5.0, extra_rows=stale)
    assert v["clean_n"] == 10, "only the in-window longs count"
    assert v["clean_alpha"] is None, "under 20 clean rows -> no claim at all"
    assert v["verdict"] != "ON TRACK", "a gauge with no data cannot be a passing gauge"

    fresh = _verdict(cfg, alphas=[1.0] * 20, monthly=5.0, extra_rows=stale)
    assert fresh["clean_n"] == 20 and fresh["clean_alpha"] == pytest.approx(1.0)


@pytest.mark.invariant
def test_clean_gauge_drops_absurd_alphas_and_trims_the_tails(cfg):
    """Two separate defenses against one corrupt print dominating the gauge:
    rows with |alpha| > 60 are dropped outright (a reverse-split artifact, not a trade), and the
    surviving sample is 10%-trimmed at both tails before averaging.

    Untrimmed, the sample below averages +6.8%/trade; trimmed it is the honest +1.0.
    """
    base = [1.0] * 18 + [59.0, 59.0]
    assert sum(base) / len(base) == pytest.approx(6.8), "the untrimmed mean this test is beating"
    v = _verdict(cfg, alphas=base, monthly=5.0,
                 extra_rows=_explore_rows([5000.0, -900.0, 60.01]))
    assert v["clean_n"] == 20, "|alpha| > 60 rows never enter the sample"
    assert v["clean_alpha"] == pytest.approx(1.0), "10% trimmed both tails, not the 6.8 raw mean"

    kept = _verdict(cfg, alphas=base, monthly=5.0, extra_rows=_explore_rows([60.0]))
    assert kept["clean_n"] == 21, "exactly 60 is inside the cut — the bound is |alpha| > 60"


# --------------------------------------------------------------------------------------------
# 4-6. persistence hygiene
# --------------------------------------------------------------------------------------------
@pytest.mark.invariant
def test_scrub_heals_non_finite_at_any_depth_and_counts_it(cfg):
    """_scrub replaces NaN / +inf / -inf with 0.0 at ANY nesting depth, counts what it healed, and
    leaves every finite value (and every non-float) exactly as it was.
    """
    obj = {"top": NAN,
           "list": [1.5, INF, {"deep": -INF, "ok": 2.5}],
           "nested": {"a": {"b": [NAN, 0.0]}},
           "keep": {"i": 7, "s": "0.0", "b": True, "none": None, "f": -3.25}}
    stats: dict = {}
    out = persist._scrub(obj, stats)
    assert stats["healed"] == 4, "one count per non-finite float, wherever it sits"
    assert out["top"] == 0.0
    assert out["list"] == [1.5, 0.0, {"deep": 0.0, "ok": 2.5}]
    assert out["nested"]["a"]["b"] == [0.0, 0.0]
    assert out["keep"] == {"i": 7, "s": "0.0", "b": True, "none": None, "f": -3.25}
    assert all(math.isfinite(x) for x in (out["top"], out["list"][1], out["nested"]["a"]["b"][0]))
    assert persist._scrub({"clean": 1.0}, {}) == {"clean": 1.0}


@pytest.mark.invariant
def test_load_json_heals_a_file_written_with_nan_by_a_previous_process(capsys):
    """json.dump happily writes bare NaN/Infinity tokens, and json.load happily reads them back — so
    a poisoned accumulator survives every restart. load_json heals them to 0.0 on the way in and
    announces the count. Live boot healed 50 non-finite values in knowledge.json and 6 in sim.json.
    """
    p = "state/knowledge.json"
    with open(p, "w") as f:
        json.dump({"sources": {"google_news": {"trades": 12, "sum_ret": NAN}},
                   "sim": {"sum_ret": INF, "episodes": 3}}, f)   # allow_nan defaults to True
    assert "NaN" in open(p).read(), "the on-disk poison this test is about"

    obj = persist.load_json(p, None)
    assert obj["sources"]["google_news"]["sum_ret"] == 0.0
    assert obj["sim"]["sum_ret"] == 0.0
    assert obj["sim"]["episodes"] == 3, "finite neighbours are untouched"
    assert "healed 2 non-finite value(s) in knowledge.json" in capsys.readouterr().out


@pytest.mark.rail
def test_load_json_falls_back_to_the_bak_when_the_main_file_is_corrupt():
    """A kill mid-write must never lose the son's progress: a truncated/garbage main file falls back
    to the last-good .bak, and only a total absence returns the caller's default.
    """
    persist.save_json("state/ladder.json", {"rung": 3, "paper_equity": 411.0})
    persist.save_json("state/ladder.json", {"rung": 4, "paper_equity": 512.0})
    with open("state/ladder.json", "w") as f:
        f.write('{"rung": 4, "paper_eq')                      # killed mid-write

    assert persist.load_json("state/ladder.json", {"rung": 0}) == {"rung": 3, "paper_equity": 411.0}
    assert persist.load_json("state/nothing_here.json", {"rung": 0}) == {"rung": 0}


@pytest.mark.rail
def test_save_json_is_atomic_and_keeps_the_previous_good_copy():
    """Every save swaps in atomically (no reader ever sees a half file, no .tmp left behind) and
    demotes the previous contents to .bak — the copy the fallback above depends on.
    """
    persist.save_json("state/sim.json", {"episodes": 1, "sum_ret": 10.0})
    persist.save_json("state/sim.json", {"episodes": 2, "sum_ret": 20.0})

    assert json.load(open("state/sim.json")) == {"episodes": 2, "sum_ret": 20.0}
    assert json.load(open("state/sim.json.bak")) == {"episodes": 1, "sum_ret": 10.0}
    assert not os.path.exists("state/sim.json.tmp"), "the temp file is replaced, never orphaned"

    persist.save_json("state/sim.json", {"episodes": 3, "sum_ret": 30.0})
    assert json.load(open("state/sim.json.bak")) == {"episodes": 2, "sum_ret": 20.0}, "bak rolls"


@pytest.mark.invariant
@pytest.mark.invariant
def test_save_json_should_not_write_non_finite_tokens_to_disk():
    """A saved state file must never contain a non-finite token.

    Healing on load is the second line of defence, not the only one: save_json used to call
    json.dump with the default allow_nan=True, writing the bare tokens NaN / Infinity (invalid
    JSON) that any consumer using a plain json.load reads back as poison. Fixed 2026-09-02 with a
    scrub on the way out plus allow_nan=False. See earshot/persist.py:14-40.
    """
    persist.save_json("state/knowledge.json", {"sources": {"x": {"sum_ret": NAN, "wins": 1}}})
    raw = open("state/knowledge.json").read()
    assert "NaN" not in raw and "Infinity" not in raw, f"non-finite token written to disk: {raw!r}"


# --------------------------------------------------------------------------------------------
# 7-8. the strategy tournament: a fold, and the luck floor, must stay finite
# --------------------------------------------------------------------------------------------
@pytest.mark.invariant
def test_bump_refuses_non_finite_and_heals_an_already_poisoned_bin(cfg):
    """A NaN/inf trade result must never enter a walk-forward fold, and a bin already carrying a NaN
    ``u`` from an older run is healed to 0.0 on the next real bump instead of staying poisoned
    forever. Six NaN folds made the multiple-testing luck floor NaN, silently disabling the overfit
    rejection.
    """
    book = StrategyBook(cfg, _core(cfg))

    fresh = {"n": 0, "w": 0, "u": 0.0}
    for bad in (NAN, INF, -INF, 1e9):
        book._bump(fresh, bad)
    assert fresh == {"n": 0, "w": 0, "u": 0.0}, "not one poisoned result was counted"

    book._bump(fresh, -2.0)
    assert fresh["n"] == 1 and fresh["w"] == 0
    assert fresh["u"] == pytest.approx(-2.0 * float(cfg["survival"]["loss_aversion_lambda"]))

    poisoned = {"n": 5, "w": 2, "u": NAN}
    book._bump(poisoned, 2.0)
    assert math.isfinite(poisoned["u"]), "the NaN bin is healed, not carried"
    assert poisoned["u"] == pytest.approx(round(2.0 / 6, 4))
    assert poisoned["n"] == 6 and poisoned["w"] == 3


@pytest.mark.invariant
def test_luck_threshold_filters_non_finite_scores_and_stays_finite(cfg):
    """The deflated-Sharpe multiple-testing floor is the overfit rejection. A single NaN fold made
    the whole floor NaN, and every ``score <= floor`` comparison then evaluated False — so the gate
    waved everything through. Non-finite scores are filtered out and the floor comes back finite.
    """
    book = StrategyBook(cfg, _core(cfg))

    def strat(sid, us):
        return {"def": {"id": sid, "style": "momentum", "stop": 6, "target": 12, "hold": 5},
                "n": 500, "reg": {}, "cal": True,
                "folds": {str(i): {"n": 40, "w": 20, "u": u} for i, u in enumerate(us)}}

    book.stats.clear()
    for i, u in enumerate((0.4, 0.9, -0.3, 1.4, -1.1)):
        book.stats[f"S{i}"] = strat(f"S{i}", [u, u, u, u])
    for i, bad in enumerate((NAN, NAN, INF)):
        book.stats[f"P{i}"] = strat(f"P{i}", [0.5, bad, 0.5, 0.5])

    poisoned_score = book._score(book.stats["P0"])
    assert poisoned_score != poisoned_score, "the poisoned strategy really does score NaN"

    floor = book._luck_threshold()
    assert math.isfinite(floor), "a NaN floor disables the overfit rejection entirely"
    assert floor > 0, "with 5 clean scores the multiple-testing bar is a real, positive bar"

    book.stats.pop("S4")                                   # only 4 finite scores left
    assert book._luck_threshold() == 0.0, "too few samples -> an honest 0.0, still never NaN"


# --------------------------------------------------------------------------------------------
# 9-11. NaN at the sources: the scout, the bar cache, the sim
# --------------------------------------------------------------------------------------------
@pytest.mark.invariant
def test_scout_report_heals_a_nan_sum_ret_instead_of_propagating_it():
    """A source whose lifetime sum_ret went NaN (google_news, form4) reported avg_ret NaN into the
    coach digest and the source-weighting fusion. report() heals the stored accumulator to 0.0 and
    emits a finite avg_ret, while a healthy source is left exactly alone.
    """
    know = {"sources": {"google_news": {"trades": 40, "wins": 14, "sum_ret": NAN},
                        "sec_form4": {"trades": 10, "wins": 6, "sum_ret": 25.0},
                        "quiet": {"trades": 0, "wins": 0, "sum_ret": 0.0}}}
    rep = SourceScout(know, {"google_news": 0.3}).report()

    rows = {r["source"]: r for r in rep["sources"]}
    assert set(rows) == {"google_news", "sec_form4"}, "zero-trade sources are not reported on"
    assert rows["google_news"]["avg_ret"] == 0.0
    assert math.isfinite(rows["google_news"]["avg_ret"])
    assert know["sources"]["google_news"]["sum_ret"] == 0.0, "the accumulator itself is healed"
    assert rows["sec_form4"]["avg_ret"] == pytest.approx(2.5), "a clean source is untouched"
    assert know["sources"]["sec_form4"]["sum_ret"] == 25.0
    assert "google_news" in rep["decaying"], "40 trades at 35% precision is still called decaying"


@pytest.mark.invariant
def test_history_load_drops_non_finite_and_non_positive_closes_at_the_source():
    """The bar cache is where the rot got in. load() drops NaN, inf, zero and negative closes on the
    way out of the CSV, so no downstream consumer (sim episodes, strategy folds, the luck floor) can
    ever see one. A single bad bar poisoned the sim ledger and six strategy folds.
    """
    os.makedirs(history.HIST_DIR, exist_ok=True)
    with open(history._path("BADT"), "w") as f:
        f.write("\n".join(["2026-01-02,100.0", "2026-01-03,nan", "2026-01-04,inf",
                           "2026-01-05,-inf", "2026-01-06,0", "2026-01-07,-5.0",
                           "2026-01-08,101.5"]))

    bars = history.load("BADT")
    assert bars == [("2026-01-02", 100.0), ("2026-01-08", 101.5)]
    assert all(math.isfinite(c) and c > 0 for _, c in bars)
    assert history.load("NEVER_DOWNLOADED") == [], "a missing cache degrades to empty, not a crash"


@pytest.mark.invariant
@pytest.mark.parametrize("bad", [NAN, INF, -INF, 0.0, -1.0])
def test_episode_from_refuses_a_window_with_a_dirty_close(cfg, monkeypatch, bad):
    """A sim episode over a window containing a non-finite or non-positive close returns None. It
    used to compute a NaN return, which entered state['sum_ret'] and stayed there permanently
    ("avg nan%"), poisoning the benchmark edge and the growth ledger downstream.
    """
    monkeypatch.setattr(marketadj, "market_return", lambda a, b: 0.0)
    eng = SimEngine(cfg, learner=None)
    dates = [f"2026-01-{d:02d}" for d in range(1, 32)] + [f"2026-02-{d:02d}" for d in range(1, 70)]
    closes = [100.0] * 100
    i = 60

    clean = eng._episode_from("TST", list(zip(dates, closes)), i, 5.0)
    assert clean is not None and math.isfinite(clean["ret"]) and math.isfinite(clean["alpha"])

    closes[i + 2] = bad                                    # inside the inspected window
    assert eng._episode_from("TST", list(zip(dates, closes)), i, 5.0) is None

    far = [100.0] * 100
    far[i - 60] = bad                                      # outside it — must not block a good episode
    assert eng._episode_from("TST", list(zip(dates, far)), i, 5.0) is not None


@pytest.mark.invariant
def test_sim_report_stays_finite_after_the_engine_heals_its_own_state(cfg):
    """The sim's visible scorecard must never print NaN. A sim.json whose sim_equity went non-finite
    (or ran away past the cap, the $1e60 overfit artifact) is healed back to the starting stake on
    construction, and report() divides only by finite numbers.
    """
    start = float(cfg["survival"]["starting_capital"])
    persist.save_json("state/sim.json", {"episodes": 40, "wins": 18, "sum_ret": 33.0,
                                         "sim_equity": INF, "peak": INF, "start": start,
                                         "last_ts": 0, "windows": [], "eps_since_reset": 9})
    rep = SimEngine(cfg, learner=None).report()
    assert rep["sim_equity"] == start and rep["grown_pct"] == 0.0
    assert all(math.isfinite(rep[k]) for k in ("win_rate", "avg_ret", "sim_equity", "grown_pct"))
    assert rep["avg_ret"] == pytest.approx(round(33.0 / 40, 3))


# --------------------------------------------------------------------------------------------
# 12. the money gauge itself
# --------------------------------------------------------------------------------------------
@pytest.mark.invariant
def test_monthly_return_pct_refuses_to_extrapolate_from_a_few_hours(cfg):
    """The realized money gauge compounds a curve to a 30-day month, so a couple of hours of noise
    would annualize into a fantasy. Below 3 days elapsed (or with no usable start equity) it returns
    None — and velocity() treats None as a FAILED money gauge, never a passing one.
    """
    inst = MotherInstinct(cfg)
    now = time.time()

    inst.state["equity_curve"] = [{"t": now - 3600, "eq": 500.0}, {"t": now, "eq": 530.0}]
    assert inst.monthly_return_pct() is None, "one hour of +6% must not become +4000%/month"

    inst.state["equity_curve"] = [{"t": now - 2.9 * 86400, "eq": 500.0}, {"t": now, "eq": 530.0}]
    assert inst.monthly_return_pct() is None, "the honest minimum is 3 days"

    inst.state["equity_curve"] = [{"t": now - 10 * 86400, "eq": 500.0}, {"t": now, "eq": 600.0}]
    assert inst.monthly_return_pct() == pytest.approx(72.8), "(1.2 ** 3 - 1) * 100, compounded"

    inst.state["equity_curve"] = [{"t": now - 10 * 86400, "eq": 0.0}, {"t": now, "eq": 600.0}]
    assert inst.monthly_return_pct() is None, "no division by a dead starting equity"

    inst.state["equity_curve"] = [{"t": now, "eq": 500.0}]
    assert inst.monthly_return_pct() is None, "a single point is not a curve"


# --------------------------------------------------------------------------------------------
# 13-14. the growth ledger and the learned-rule merge
# --------------------------------------------------------------------------------------------
@pytest.mark.invariant
def test_record_day_keeps_one_row_per_day_and_caps_the_ledger(cfg):
    """The growth ledger is the input to every mastery/velocity delta. It must hold exactly one row
    per calendar day (a re-run the same day OVERWRITES, never appends a second row, which would make
    a day-over-day delta compare a day against itself) and stay bounded at 180 days.
    """
    core = _core(cfg)
    core.record_day("2026-08-31", {"mastery": 10.0, "samples": 100})
    core.record_day("2026-08-31", {"mastery": 11.0, "samples": 120})
    hist = core.know["growth"]["history"]
    assert len(hist) == 1 and hist[0]["mastery"] == 11.0, "latest run of the day wins"

    traj = core.record_day("2026-09-01", {"mastery": 12.0, "samples": 140})
    assert len(core.know["growth"]["history"]) == 2
    assert traj["mastery_dod"] == pytest.approx(1.0), "the delta is day vs the PREVIOUS day"

    for d in range(1, 201):
        core.record_day(f"day-{d:03d}", {"mastery": float(d)})
    hist = core.know["growth"]["history"]
    assert len(hist) == 180, "~6 months, bounded — the ledger cannot grow without limit"
    assert hist[-1]["day"] == "day-200", "the cap drops the OLDEST rows, never the newest"


@pytest.mark.invariant
def test_generic_default_rule_may_only_fill_missing_keys(cfg):
    """The learned "__default__" rule (a daily-bar momentum sweep with nothing to do with catalysts)
    was the only key that ever existed, so it silently overwrote EVERY catalyst's exit geometry:
    insider_cluster's 30-day hold, fda's 10% stop and pdufa's 35-day hold all became {6/16/5}.
    Now a catalyst-specific learned rule still wins, but the generic one may only fill keys the
    config left missing.
    """
    core = _core(cfg)
    core.know["catalyst_rules"]["__default__"] = {"stop_pct": 6, "target_pct": 16, "hold_days": 5}

    ins = dict(cfg["catalysts"]["insider_cluster"])
    assert core.rule("insider_cluster", ins) == ins, "the generic sweep overwrites NOTHING"
    assert core.rule("pdufa_runup", dict(cfg["catalysts"]["pdufa_runup"]))["hold_days"] == 35

    core.know["catalyst_rules"]["insider_cluster"] = {"stop_pct": 14, "hold_days": 25}
    learned = core.rule("insider_cluster", ins)
    assert (learned["stop_pct"], learned["hold_days"]) == (14, 25), "a real learned rule still wins"
    assert learned["target_pct"] == ins["target_pct"], "keys it didn't learn keep the config value"

    partial = core.rule("gap_fill", {"stop_pct": 9})
    assert partial == {"stop_pct": 9, "target_pct": 16, "hold_days": 5}, "fills only what was missing"
