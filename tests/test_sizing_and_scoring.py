"""Position sizing and the score pipeline.

Every test here pins a finding from the autopsy of 2026-09-01 (fix pass "Phase 1", commit 4e4b08c).
The bot's own self-coder commits to main daily and has flip-flopped a single constant twelve times,
so these are the tripwires: if a future edit re-inverts the sizing math or lets a multiplier saturate
the score again, this file goes red before any capital is deployed.

Nothing here touches the network, the real state/, or an LLM. Every module under test is constructed
by hand from plain dicts so the arithmetic being asserted is the shipped arithmetic.
"""
from __future__ import annotations

import math

import pytest

from earshot.brain import Brain
from earshot.core import BrainCore
from earshot.patterns import PatternMemory
from earshot.survival import MotherInstinct, compute_mastery


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------

def _mother(cfg, equity: float) -> MotherInstinct:
    """A MotherInstinct whose paper equity is exactly `equity`. State lives in the throwaway cwd."""
    m = MotherInstinct(cfg)
    m.state["paper_equity"] = float(equity)
    return m


def _patterns(grad: dict) -> PatternMemory:
    """A PatternMemory with a hand-built graduation ledger and NO ledger rebuild from disk."""
    know = {"patterns_grad": dict(grad), "patterns_grad_src": "book"}
    return PatternMemory(know)


class _FakeGate:
    """A P(win) model that is trained, perfectly trusted, and maximally bullish. The point of the
    stub is to prove the Kelly-lite multiplier cannot outrun the hard per-name cap."""

    def __init__(self, pwin_field: float = 0.34):
        # field_rate() reads the rolling base-rate tracker; give it >= 30 samples so it is used.
        self.know = {"base_rate": {"n": 200, "ema": pwin_field}, "sources": {}}

    def ready(self) -> bool:
        return True

    def trust(self) -> float:
        return 1.0


def _rows(returns, conf=80.0, size_frac=None):
    """Resolved-outcome rows in the shape survival.compute_mastery consumes."""
    out = []
    for r in returns:
        row = {"conf": conf, "pnl_pct": r, "alpha": r, "win": r > 0}
        if size_frac is not None:
            row["size_frac"] = size_frac
        out.append(row)
    return out


# ----------------------------------------------------------------------------------------------
# 1. sizing_capital: size off what the son ACTUALLY has
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_sizing_capital_tracks_real_equity_and_never_exceeds_the_stake(cfg):
    """In SHADOW, sizing capital is min(start, max(0.5*start, paper_equity)).

    Autopsy: the book deployed the fixed $500 stake while equity sat at $411.55, i.e. 121% deployed.
    Sizing must follow the equity down, must never exceed the original stake on the way up, and
    must floor at half the stake so a deep drawdown cannot starve the sample flow to zero.
    See earshot/survival.py:133-141.
    """
    assert cfg["identity"]["mode"] == "shadow", "this rail is the shadow-mode sizing law"
    start = float(cfg["survival"]["starting_capital"])
    assert start == 500.0

    # a) below the stake -> size off the real equity, NOT the stake
    assert _mother(cfg, 411.55).sizing_capital() == pytest.approx(411.55)
    assert _mother(cfg, 411.55).sizing_capital() < start

    # b) above the stake -> clamp back down to the stake (no compounding into leverage)
    assert _mother(cfg, 912.40).sizing_capital() == pytest.approx(start)

    # c) crushed equity -> floor at half the stake, so learning does not stall at $0 size
    assert _mother(cfg, 60.0).sizing_capital() == pytest.approx(0.5 * start)
    assert _mother(cfg, 0.0).sizing_capital() == pytest.approx(250.0)

    # and the floor is a floor, not a default: $300 is above it and passes straight through
    assert _mother(cfg, 300.0).sizing_capital() == pytest.approx(300.0)


# ----------------------------------------------------------------------------------------------
# 2. the hard per-name cap
# ----------------------------------------------------------------------------------------------

@pytest.mark.rail
@pytest.mark.invariant
def test_size_never_exceeds_max_position_frac_at_any_score(cfg):
    """No single name may take more than conviction.max_position_frac (0.25) of the sizing capital.

    Autopsy: LLY was sized at $450 of a $500 book and was 31% of all losses; the 31 max-confidence
    trades lost $71 of the $88 total. See earshot/brain.py:140-143.
    """
    cap = float(cfg["conviction"]["max_position_frac"])
    assert cap == 0.25, "the autopsy cap is 25% of capital; a wider cap must fail this suite"

    b = Brain(cfg)
    capital = 500.0
    for score in [0, 15, 29, 30, 54, 55, 69, 70, 84, 85, 94, 95, 99, 100, 100.0]:
        assert b._size(score, capital) <= round(capital * cap, 2) + 1e-9, f"score {score} broke the cap"

    # top band 0.25 == the per-name cap: 100 -> $125 either way (2026-09-02 the bands were
    # lowered to sit under the cap, so the two agree instead of the cap silently clamping)
    assert b._size(100.0, capital) == pytest.approx(125.0)
    assert b._size(96.0, capital) == pytest.approx(125.0)
    # and the cap scales with the capital passed in, it is a fraction not a constant
    assert b._size(100.0, 200.0) == pytest.approx(50.0)


@pytest.mark.rail
def test_max_position_frac_cap_is_applied_last_and_survives_the_kelly_multiplier(cfg):
    """The edge-based (Kelly-lite) size multiplier is applied BEFORE the per-name cap, so a
    maximally bullish, maximally trusted P(win) model still cannot exceed 25% of capital.

    A cap applied before the multiplier would be no cap at all. See earshot/brain.py:127-143.
    """
    b = Brain(cfg)
    b.lgate = _FakeGate()
    capital = 500.0
    # pwin far above the field -> edge saturates at +1 -> multiplier 1.5x on the 0.35 band = 0.525
    unc = b._size(100.0, capital, pwin=0.99)
    assert unc == pytest.approx(125.0), "the Kelly multiplier escaped the per-name cap"
    assert unc <= capital * float(cfg["conviction"]["max_position_frac"]) + 1e-9


@pytest.mark.rail
def test_max_position_frac_default_holds_when_the_config_key_is_deleted(cfg):
    """Deleting conviction.max_position_frac from config.yaml must NOT uncap sizing: the code
    default is the same 0.25. A self-coder that removes the knob still hits the cap.
    See earshot/brain.py:142.
    """
    cfg["conviction"].pop("max_position_frac", None)
    b = Brain(cfg)
    assert b._size(100.0, 500.0) == pytest.approx(125.0)


# ----------------------------------------------------------------------------------------------
# 3. _soft: de-saturation
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_soft_desaturates_without_collapsing_the_ranking():
    """_soft compresses above 80 instead of clamping at 100, so distinct raw scores stay distinct.

    Autopsy: ~12 stacked multipliers pushed most qualifying names into the hard 100 clamp, so
    ranking collapsed to a tiebreak and the TOP size band fired on ties. See earshot/brain.py:145-154.
    """
    # identity at or below 80 (the compression must not touch ordinary scores)
    for s in (0.0, 15.0, 42.5, 79.9, 80.0):
        assert Brain._soft(s) == pytest.approx(s)

    # 100 maps to ~88, not 100
    assert Brain._soft(100.0) == pytest.approx(80.0 + 20.0 * (1 - math.exp(-0.5)), abs=1e-9)
    assert 87.0 < Brain._soft(100.0) < 89.0

    # the headroom case: three raw scores that ALL used to clamp to 100 stay strictly ordered
    a, bb, c = Brain._soft(100.0), Brain._soft(120.0), Brain._soft(200.0)
    assert a < bb < c, "distinct raw scores piled onto the same output; ranking is dead"
    assert c < 100.0, "200 still saturates the clamp"
    assert bb - a > 3.0 and c - bb > 3.0, "the spread is too small to rank on"

    # strictly monotone increasing across the whole domain, and never above 100
    xs = [i * 0.5 for i in range(0, 1200)]
    ys = [Brain._soft(x) for x in xs]
    assert all(y2 > y1 for y1, y2 in zip(ys, ys[1:])), "_soft is not strictly monotone"
    assert max(ys) <= 100.0
    assert Brain._soft(-50.0) == 0.0


# ----------------------------------------------------------------------------------------------
# 4. core.rule: the generic __default__ may only FILL
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_generic_default_rule_cannot_overwrite_a_catalyst_specific_config(cfg):
    """A learned "__default__" rule may only fill keys the catalyst config LACKS, never replace them.

    Autopsy: "__default__" (a daily-bar momentum sweep, nothing to do with catalysts) was the only
    key that ever existed, so {stop 6, target 16, hold 5} silently replaced insider_cluster's 30-day
    hold and 12% stop, fda_approval's 10% stop and pdufa_runup's 35-day hold. See earshot/core.py:72-86.
    """
    know = {"catalyst_rules": {"__default__": {"stop_pct": 6, "target_pct": 16, "hold_days": 5}}}
    core = BrainCore(know, {}, cfg)

    ins = cfg["catalysts"]["insider_cluster"]
    assert (ins["hold_days"], ins["stop_pct"]) == (30, 12), "config drifted; the fixture below is stale"
    r = core.rule("insider_cluster", dict(ins))
    assert r["hold_days"] == 30, "the generic sweep ate the 30-day insider drift horizon"
    assert r["stop_pct"] == 12, "the generic 6% stop replaced the wide insider stop"
    assert r["target_pct"] == 20

    # the same for the other long-horizon catalysts the generic rule was crushing
    assert core.rule("pdufa_runup", dict(cfg["catalysts"]["pdufa_runup"]))["hold_days"] == 35
    assert core.rule("fda_approval", dict(cfg["catalysts"]["fda_approval"]))["stop_pct"] == 10

    # FILL is still allowed: keys the config genuinely lacks come from the generic rule
    filled = core.rule("insider_cluster", {"prior": 1.0})
    assert (filled["stop_pct"], filled["target_pct"], filled["hold_days"]) == (6, 16, 5)


@pytest.mark.invariant
def test_catalyst_specific_learned_rule_beats_both_config_and_generic_default(cfg):
    """A learned rule for THIS catalyst still wins over the config, key by key, and the generic
    "__default__" never gets a second bite at the keys the config already supplied.
    See earshot/core.py:79-85.
    """
    know = {"catalyst_rules": {
        "insider_cluster": {"stop_pct": 9},
        "__default__": {"stop_pct": 6, "target_pct": 16, "hold_days": 5},
    }}
    core = BrainCore(know, {}, cfg)
    r = core.rule("insider_cluster", dict(cfg["catalysts"]["insider_cluster"]))
    assert r["stop_pct"] == 9, "the catalyst's own learned stop must win"
    assert r["hold_days"] == 30, "the generic hold leaked in behind the learned stop"
    assert r["target_pct"] == 20


# ----------------------------------------------------------------------------------------------
# 5. calib_factor may only haircut
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_calibration_can_only_haircut_never_inflate(cfg):
    """calib_factor is bounded above by 1.0 on both the per-bin and the legacy global path.

    Autopsy: every live calibration bin sat ABOVE 1.0 (the 85+ bin at 1.09 while that bucket won
    only 43%), so "calibration" was inflating scores into the 100 clamp instead of correcting them.
    See earshot/core.py:88-102.
    """
    inflating = BrainCore({"calibration": {"per_bin": {"55": 1.30, "70": 1.15, "85": 1.09}}}, {}, cfg)
    assert inflating.calib_factor(60.0) == 1.0
    assert inflating.calib_factor(80.0) == 1.0
    assert inflating.calib_factor(95.0) == 1.0, "the 1.09 85-bin is inflating conviction again"

    # a genuine haircut passes through untouched, and lands in the right bucket
    haircut = BrainCore({"calibration": {"per_bin": {"55": 0.80, "70": 0.60, "85": 0.40}}}, {}, cfg)
    assert haircut.calib_factor(69.9) == pytest.approx(0.80)   # <70  -> "55"
    assert haircut.calib_factor(70.0) == pytest.approx(0.60)   # <85  -> "70"
    assert haircut.calib_factor(85.0) == pytest.approx(0.40)   # else -> "85"

    # legacy pre-fix knowledge.json carried a single global scalar; it is clamped too
    assert BrainCore({"calibration": {"factor": 1.25}}, {}, cfg).calib_factor(90.0) == 1.0
    assert BrainCore({"calibration": {"factor": 0.70}}, {}, cfg).calib_factor(90.0) == pytest.approx(0.70)
    # and no calibration data at all is a no-op, not a boost
    assert BrainCore({}, {}, cfg).calib_factor(90.0) == 1.0


# ----------------------------------------------------------------------------------------------
# 6. size-weighted drawdown
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_mastery_drawdown_is_size_weighted(cfg):
    """max_dd compounds each trade's % scaled by the fraction of equity it actually risked.

    Autopsy: compounding every trade at 100% of equity reported a 43% drawdown on a book whose
    real drawdown was 18%. See earshot/survival.py:84-91.
    """
    seq = [8.0, -12.0, -15.0, 5.0, -10.0, 6.0, -9.0]
    full = compute_mastery(_rows(seq, size_frac=1.0), 500.0)
    quarter = compute_mastery(_rows(seq, size_frac=0.25), 500.0)

    assert quarter["max_dd"] < full["max_dd"], "drawdown ignored position size"
    assert quarter["max_dd"] < 0.5 * full["max_dd"], (
        f"quarter-sized book reported {quarter['max_dd']:.3f} dd vs full-size {full['max_dd']:.3f}; "
        "the weighting is not materially reducing risk")
    # the size term only touches drawdown; the skill terms are identical
    assert quarter["profit_factor"] == full["profit_factor"]
    assert quarter["win_rate"] == full["win_rate"]
    assert quarter["mastery"] > full["mastery"], "a smaller real drawdown must not score worse"


@pytest.mark.invariant
def test_legacy_rows_without_size_frac_default_to_a_quarter_position(cfg):
    """A resolved row that predates the size_frac field is assumed to be a quarter position (the
    typical band), not a full-equity bet. See earshot/survival.py:89.
    """
    seq = [8.0, -12.0, -15.0, 5.0, -10.0, 6.0, -9.0]
    legacy = compute_mastery(_rows(seq), 500.0)                      # no size_frac key at all
    quarter = compute_mastery(_rows(seq, size_frac=0.25), 500.0)
    full = compute_mastery(_rows(seq, size_frac=1.0), 500.0)

    assert legacy["max_dd"] == pytest.approx(quarter["max_dd"])
    assert legacy["mastery"] == pytest.approx(quarter["mastery"])
    assert legacy["max_dd"] != pytest.approx(full["max_dd"])


# ----------------------------------------------------------------------------------------------
# 7. no skill for free
# ----------------------------------------------------------------------------------------------

@pytest.mark.rail
@pytest.mark.invariant
def test_a_losing_book_cannot_clear_the_rung_zero_promotion_bar(cfg):
    """A book with profit factor below 1 and negative expectancy must NOT reach the rung-0 bar.

    The real shape at the autopsy: ~51% alpha-win rate, profit factor 0.72, mean alpha -0.5%.
    Winning slightly more than half the time while losing money is exactly the state the ladder
    must refuse to promote. See earshot/survival.py:55-106 and config.yaml survival.ladder rung 0.
    """
    rung0 = next(r for r in cfg["survival"]["ladder"] if r["rung"] == 0)
    bar = float(rung0["promote_mastery"])
    assert bar == 60, "the rung-0 bar moved; re-derive this test against the new bar"

    # 51 winners of +2.521%, 49 losers of -3.644% -> pf 0.72, mean alpha -0.50
    recent = _rows([2.521] * 51 + [-3.644] * 49, conf=80.0, size_frac=0.25)
    m = compute_mastery(recent, 500.0)

    assert m["n"] == 100
    assert m["win_rate"] == pytest.approx(0.51, abs=0.01)
    assert m["profit_factor"] == pytest.approx(0.72, abs=0.02), "the fixture no longer models pf 0.72"
    assert m["expectancy"] == pytest.approx(-0.50, abs=0.05), "the fixture no longer models -0.5 alpha"
    assert m["mastery"] < bar, (
        f"a pf-{m['profit_factor']} book with {m['expectancy']}% mean alpha scored "
        f"{m['mastery']} against a promotion bar of {bar}")

    # sub-1 profit factor contributes exactly zero, and negative expectancy contributes zero.
    # The score that remains is sample credit + calibration, not skill.
    assert m["profit_factor"] < 1.0
    assert m["overconfidence"] > 0.0, "believing 0.80 while delivering 0.51 must be braked"


# ----------------------------------------------------------------------------------------------
# 8. the shipped size bands
# ----------------------------------------------------------------------------------------------

@pytest.mark.rail
@pytest.mark.invariant
def test_size_bands_are_capped_and_monotone_in_config(raw_config):
    """conviction.size_bands stay at or under the per-name cap and never invert.

    Autopsy: the top band was frac 1.00 (the whole book on one name). A config edit that restores
    a full-equity band must fail here, before it can ever reach the sizer. Follow-up 2026-09-02:
    bands of 0.35/0.30 sat ABOVE max_position_frac (0.25), so the cap clamped them and scores 70
    through 100 all sized identically at $125 — conviction bought nothing above 70. Bands now sit
    at or under the cap and every one is distinct. See config.yaml and earshot/brain.py:117-143.
    """
    bands = raw_config["conviction"]["size_bands"]
    assert bands, "the size bands vanished from config"

    mins = [float(b["min"]) for b in bands]
    fracs = [float(b["frac"]) for b in bands]

    cap = float(raw_config["conviction"]["max_position_frac"])
    assert max(fracs) <= cap, f"a size band exceeds the per-name cap {cap}: {max(fracs)}"
    assert all(f > 0 for f in fracs), "a zero-frac band can never trade"
    # brain._size takes the FIRST band whose min the score clears, so mins must descend
    assert mins == sorted(mins, reverse=True), "bands are not ordered by descending score"
    # and more conviction must never buy less
    assert fracs == sorted(fracs, reverse=True), "size is not monotone in score"
    # 2026-09-02: the ladder must actually DIFFERENTIATE under the cap. Bands that all clamp to
    # the same number mean conviction buys nothing: scores 70..100 previously all sized $125.
    assert len(set(fracs)) == len(fracs), "duplicate band fracs — conviction stops differentiating"
    # every band must survive the cap intact, so a raised score always buys a bigger position
    assert all(f <= cap for f in fracs), "a band is unreachable because the cap clamps it"


# ----------------------------------------------------------------------------------------------
# 9. _score
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_score_is_bounded_and_rewards_cross_family_convergence(cfg):
    """_score stays inside 0..100 under any input and rises with the number of AGREEING families.

    Cross-family agreement (filing + surprise + flow) is the actual edge; two of the same kind is
    not. The bonus is bounded at 1.5x so convergence cannot alone saturate the score.
    See earshot/brain.py:105-113.
    """
    b = Brain(cfg)
    strengths = {"edgar_8k": 1.0, "finnhub_earnings": 1.0, "jets": 0.8}

    one = b._score(strengths, prior=1.0, earliness=1.0, n_families=1)
    two = b._score(strengths, prior=1.0, earliness=1.0, n_families=2)
    three = b._score(strengths, prior=1.0, earliness=1.0, n_families=3)
    assert one < two < three, "extra agreeing families did not raise the score"
    fbonus = float(cfg["conviction"]["family_bonus"])
    assert two == pytest.approx(one * (1 + fbonus))

    # the convergence multiplier is capped at 1.5x: family #5 onward buys nothing
    assert b._score(strengths, 1.0, 1.0, 8) == pytest.approx(b._score(strengths, 1.0, 1.0, 20))

    # bounded 0..100 no matter how absurd the inputs get
    huge = {f"src{i}": 5.0 for i in range(50)}
    assert b._score(huge, prior=5.0, earliness=1.5, n_families=20) <= 100.0
    assert b._score({}, prior=1.0, earliness=1.0, n_families=1) == 0.0
    assert b._score(strengths, prior=-3.0, earliness=1.0, n_families=1) == 0.0

    # and it is monotone in raw evidence
    weak = b._score({"edgar_8k": 0.3}, 1.0, 1.0, 1)
    strong = b._score({"edgar_8k": 1.5}, 1.0, 1.0, 1)
    assert 0.0 < weak < strong < 100.0


# ----------------------------------------------------------------------------------------------
# 10. the evidence cap
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_evidence_cap_holds_a_dollar_negative_catalyst_to_a_quarter_of_capital(cfg):
    """A $-negative, ungraduated catalyst caps at conviction.unproven_position_frac, independently
    of the per-name cap. Big size must be EARNED by the ledger, not by self-assessed conviction.

    2026-09-02: this cap used to hardcode the same 0.25 as max_position_frac, so it could never
    bind on any input — an unproven catalyst sized exactly like a proven one. It now has its own
    tighter knob (0.15). See earshot/brain.py:133-143.
    """
    # widen the per-name cap so the EVIDENCE cap is the only thing that can bind
    cfg["conviction"]["max_position_frac"] = 1.0
    b = Brain(cfg)
    capital = 500.0

    # sanity: with the per-name cap off and no pattern memory, the top band pays out in full
    top = float(cfg["conviction"]["size_bands"][0]["frac"])
    assert b._size(100.0, capital) == pytest.approx(capital * top)

    # a catalyst that has lost the book real dollars over its own trades
    b.patterns = _patterns({"mna_target": {"trades": 12, "wins": 4, "usd": -50.0},
                            "loser": {"trades": 100, "wins": 20, "usd": -200.0}})
    assert not b.patterns.catalyst_graduated("mna_target")
    unproven = float(cfg["conviction"]["unproven_position_frac"])
    assert unproven < top, "the evidence cap must be TIGHTER than the top band or it cannot bind"
    assert b._size(100.0, capital, catalyst="mna_target") == pytest.approx(capital * unproven), (
        "the evidence cap let an unproven, $-negative catalyst take the full top band")

    # a GRADUATED catalyst keeps the full band: the cap is evidence-based, not a blanket haircut
    b.patterns = _patterns({"mna_target": {"trades": 30, "wins": 25, "usd": 320.0},
                            "loser": {"trades": 100, "wins": 20, "usd": -200.0}})
    assert b.patterns.catalyst_graduated("mna_target"), "fixture no longer graduates; re-derive it"
    assert b._size(100.0, capital, catalyst="mna_target") == pytest.approx(capital * top)


# ----------------------------------------------------------------------------------------------
# adjacent: the two pattern multipliers that feed the pipeline
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_learned_payoff_is_a_ceiling_on_the_target_never_a_floor():
    """patterns.suggested_target may only LOWER the config target, never raise it above it.

    Autopsy: the old max(4.0, ...) floor put a ~4% profit target under a 6% stop on every book
    trade. 22 stops at -6.30% = -$173 against 18 targets at +4.59% = +$76. See earshot/patterns.py:191-201.
    """
    pm = _patterns({})
    # a pattern whose winners average +3.0% cannot invent a 20% target out of a 12% config default
    pm.p["mna_target|filing+news"] = {"trades": 20, "wins": 10, "sum_ret": 5.0, "sum_win_ret": 30.0}
    assert pm.suggested_target("mna_target|filing+news", 12.0) == pytest.approx(3.0)
    # and a pattern whose winners average +18% is still CEILINGED by the config default
    pm.p["fda_approval|regulatory"] = {"trades": 20, "wins": 10, "sum_ret": 90.0, "sum_win_ret": 180.0}
    assert pm.suggested_target("fda_approval|regulatory", 12.0) == pytest.approx(12.0)
    # too few wins to learn from -> the config default stands untouched
    assert pm.suggested_target("brand_new|filing", 12.0) == pytest.approx(12.0)


@pytest.mark.invariant
def test_pattern_multiplier_is_bounded_and_neutral_without_evidence():
    """patterns.multiplier is clamped to [0.5, 1.5] and is exactly 1.0 below 5 samples.

    This is one of the ~12 stacked multipliers that used to drive scores into the 100 clamp. An
    unbounded pattern boost is how a single lucky signature saturates conviction.
    See earshot/patterns.py:155-167.
    """
    pm = _patterns({})
    assert pm.multiplier("never_seen|filing") == 1.0
    pm.p["thin|filing"] = {"trades": 4, "wins": 4, "sum_ret": 40.0, "sum_win_ret": 40.0}
    assert pm.multiplier("thin|filing") == 1.0, "4 samples of luck must buy no conviction"

    pm.p["perfect|filing"] = {"trades": 400, "wins": 400, "sum_ret": 4000.0, "sum_win_ret": 4000.0}
    pm.p["hopeless|filing"] = {"trades": 400, "wins": 0, "sum_ret": -4000.0, "sum_win_ret": 0.0}
    assert pm.multiplier("perfect|filing") <= 1.5
    assert pm.multiplier("hopeless|filing") >= 0.5
    assert pm.multiplier("perfect|filing") > pm.multiplier("hopeless|filing")


# ----------------------------------------------------------------------------------------------
# The generic-rule override path — the bug Phase 1 removed, and its second door
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_strategy_tournament_adopts_into_default_not_every_catalyst(cfg):
    """A tournament blend may only write the __default__ FALLBACK, never per-catalyst keys.

    Autopsy (2026-09-02): since the 09-01 fix a per-catalyst rule OUTRANKS the config, so
    `adopt_blend` stamping its blend into every key in cfg["catalysts"] would have silently
    clobbered insider_cluster's 30-day hold, pdufa's 35-day hold and fda_approval's 10% stop with
    one generic daily-bar momentum rule. That is the exact defect Phase 1 removed (a generic sweep
    overriding catalyst physics), re-entering through a second door. Inert in production today
    only because the tournament has never had a walk-forward survivor.
    See earshot/strategies.py:276-283 and earshot/core.py:72-86.
    """
    from earshot.strategies import StrategyBook

    know = {"catalyst_rules": {}, "sources": {}, "weights": {}, "patterns": {}}
    core = BrainCore(know, {}, cfg)
    book = StrategyBook(cfg, core)

    # Force a survivor so adopt_blend reaches its write path regardless of tournament state.
    winner = {"def": {"style": "momentum", "stop": 6, "target": 16, "hold": 5,
                      "uptrend": True, "min_move": 3}}
    book.survivors = lambda k=5: [winner] * 3

    blend = book.adopt_blend(cfg)
    assert blend is not None, "the forced survivor should have produced a blend"

    rules = know["catalyst_rules"]
    assert "__default__" in rules, "the blend must land on the fallback key"
    leaked = sorted(k for k in rules if k != "__default__")
    assert not leaked, f"the blend must not be stamped onto catalysts, but wrote: {leaked}"

    # ...and the config's own catalyst physics must survive the adoption untouched.
    ins = core.rule("insider_cluster", cfg["catalysts"]["insider_cluster"])
    assert ins["hold_days"] == cfg["catalysts"]["insider_cluster"]["hold_days"] == 30
    assert ins["stop_pct"] == cfg["catalysts"]["insider_cluster"]["stop_pct"] == 12


@pytest.mark.invariant
def test_min_score_to_trade_can_actually_produce_a_tradeable_size(raw_config):
    """The conviction bar must sit at or above the lowest size band, and that band must clear the
    $25 execution floor. Otherwise the bar is a lie.

    Autopsy (2026-09-02): min_score_to_trade was 15 while the lowest band started at 30, so every
    score in 15..29 sized to $0 and was then dropped by main's `size_usd < 25` check — the config
    advertised a bar of 15 that could never trade, and the NO-TRADE line blamed capital instead.
    See config.yaml conviction and earshot/brain.py:117-121, earshot/main.py (the $25 floor).
    """
    conv = raw_config["conviction"]
    bar = float(conv["min_score_to_trade"])
    lowest_band = min(float(b["min"]) for b in conv["size_bands"])
    assert bar >= lowest_band, (
        f"min_score_to_trade {bar} sits below the lowest size band {lowest_band}: every score in "
        f"between sizes to $0 and is silently skipped")

    # and a score exactly AT the bar must buy something the executor will actually take
    b = Brain(raw_config)
    start = float(raw_config["survival"]["starting_capital"])
    assert b._size(bar, start) >= 25.0, (
        f"a score at the bar sizes {b._size(bar, start)} on a ${start:.0f} book, under the $25 floor")
