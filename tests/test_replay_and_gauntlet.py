"""The replay harness and the Gauntlet — the two pieces that decide whether a change helped.

Both were built on 2026-09-02 to replace machinery that measured the wrong thing: the CI fitness
gate scored one model's Brier over 400 rows (so any patch outside it shipped as "no worse by
construction"), and the Gauntlet was a June artifact testing a strategy the bot does not run while
the money gate kept reading it as evidence.

The tests below are deliberately adversarial about the FAIL direction. A harness that cannot say
"no" is worse than no harness, because it launders noise into permission to trade real money.
"""
from __future__ import annotations

import json
import time

import pytest

from earshot import backtest, replay


# ----------------------------------------------------------------------------------------------
# helpers — write the ledgers the harness reads, in the exact shape the live writers produce
# ----------------------------------------------------------------------------------------------

def _write(path: str, rows: list[dict]):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _cand(ticker: str, ts: float, *, passes: bool = True, catalyst: str = "fda_approval",
          score: float = 80.0, blocked: str | None = None) -> dict:
    return {"ts": ts, "ticker": ticker, "catalyst": catalyst, "score": score,
            "families": 2, "sources": ["fda_approvals", "newswire"], "signature": f"{catalyst}|x",
            "would_pass_live": passes, "would_pass_shadow": True,
            "blocked_by": blocked if not passes else None}


def _practice(ticker: str, ts: float, alpha: float, direction: int = 1,
              exit_reason: str = "time") -> dict:
    from datetime import datetime, timezone
    return {"ts": datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z"),
            "ticker": ticker, "dir": direction, "exit": exit_reason, "ret": alpha,
            "alpha": alpha, "cost": 0.1, "long_win": alpha > 0, "sig": "x"}


# ----------------------------------------------------------------------------------------------
# the join: a recorded sighting only counts when the market actually answered it
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_join_pairs_a_sighting_with_the_same_ticker_and_day(isolated_state):
    """A candidate is labeled by the outcome for the SAME ticker on the SAME ET day, and unlabeled
    sightings are dropped rather than counted as zero."""
    now = time.time()
    _write(replay.CAND_PATH, [_cand("AAPL", now - 3600), _cand("MSFT", now - 3600)])
    _write(replay.LEARN_OUT, [_practice("AAPL", now - 3000, 2.5)])

    rows = replay.load_labeled(30, now=now)
    assert [r["ticker"] for r in rows] == ["AAPL"], "an unlabeled sighting must not be counted"
    assert rows[0]["alpha"] == 2.5 and rows[0]["label_src"] == "practice"


@pytest.mark.invariant
def test_join_excludes_shorts_and_corrupt_prints(isolated_state):
    """Short labels and corrupt reverse-split prints can never enter the measurement.

    The account is long-only, so a short's label describes a trade that could never be taken; and
    five reverse-split shorts (LMFA -2415%, ...) summed to -6,815 of the practice ledger's -6,713
    lifetime total, which is what drove the bogus "-2.01%/trade, no edge" headline.
    """
    now = time.time()
    _write(replay.CAND_PATH, [_cand(t, now - 3600) for t in ("SHRT", "CRPT", "BIGM", "GOOD")])
    _write(replay.LEARN_OUT, [
        _practice("SHRT", now - 3000, 5.0, direction=-1),          # short side
        _practice("CRPT", now - 3000, 0.0, exit_reason="corrupt"),  # flagged corrupt
        _practice("BIGM", now - 3000, -2415.3),                     # a split print
        _practice("GOOD", now - 3000, 1.25),
    ])
    rows = replay.load_labeled(30, now=now)
    assert [r["ticker"] for r in rows] == ["GOOD"]


@pytest.mark.invariant
def test_join_respects_the_window_and_one_row_per_ticker_day(isolated_state):
    """Old sightings fall outside the window, and a ticker seen many times in a day counts once."""
    now = time.time()
    _write(replay.CAND_PATH, [
        _cand("OLD", now - 40 * 86400),
        _cand("DUP", now - 3600), _cand("DUP", now - 3500), _cand("DUP", now - 3400)])
    _write(replay.LEARN_OUT, [_practice("OLD", now - 40 * 86400, 9.0), _practice("DUP", now - 3000, 1.0)])

    rows = replay.load_labeled(30, now=now)
    assert [r["ticker"] for r in rows] == ["DUP"], "window or per-day dedupe is not holding"


def test_reader_survives_a_half_written_line_and_reads_rotated_siblings(isolated_state):
    """The ledgers rotate and the process can die mid-append; neither may break a measurement."""
    now = time.time()
    with open(replay.CAND_PATH, "w") as f:
        f.write(json.dumps(_cand("AAA", now - 3600)) + "\n")
        f.write('{"ts": broken json\n')                     # a torn final line
    _write(replay.CAND_PATH + ".20260901-1200", [_cand("BBB", now - 3600)])
    _write(replay.LEARN_OUT, [_practice("AAA", now - 3000, 1.0), _practice("BBB", now - 3000, 2.0)])

    rows = replay.load_labeled(30, now=now)
    assert sorted(r["ticker"] for r in rows) == ["AAA", "BBB"]


# ----------------------------------------------------------------------------------------------
# the measurement itself
# ----------------------------------------------------------------------------------------------

def test_expectancy_reports_mean_hit_rate_and_a_conservative_lower_bound():
    rows = [{"alpha": 2.0}, {"alpha": -1.0}, {"alpha": 3.0}, {"alpha": -2.0}]
    e = replay.expectancy(rows)
    assert e["n"] == 4 and e["alpha"] == pytest.approx(0.5) and e["hit"] == pytest.approx(0.5)
    assert 0.0 < e["wilson_lb"] < 0.5, "the lower bound must sit BELOW the raw hit rate"
    assert replay.expectancy([])["n"] == 0


@pytest.mark.invariant
def test_selected_defaults_to_the_live_verdict_and_accepts_a_counterfactual(isolated_state):
    """`selected` reads the recorded LIVE verdict by default, and a callable can ask "what if?".

    This is the whole point of recording every gate's verdict: a policy change can be scored
    against setups the bot really saw, instead of a confounded 24h global diff.
    """
    now = time.time()
    rows = [{**_cand("A", now, passes=True), "alpha": 3.0, "score": 90.0},
            {**_cand("B", now, passes=False, blocked="not graduated"), "alpha": -2.0, "score": 40.0}]
    assert [r["ticker"] for r in replay.selected(rows)] == ["A"]
    # counterfactual: a policy that only takes high scores, ignoring the recorded verdict
    assert [r["ticker"] for r in replay.selected(rows, lambda r: r["score"] >= 80)] == ["A"]
    assert replay.selected(rows, lambda r: False) == []


@pytest.mark.invariant
def test_run_reports_edge_as_policy_minus_everything_it_saw(isolated_state):
    """`edge_vs_seen` is the number a change is judged by: does the filter beat the unfiltered set?

    A selective policy that picks the losers must report a NEGATIVE edge — the harness has to be
    able to say the change made things worse, or it cannot be used as an objective.
    """
    now = time.time()
    _write(replay.CAND_PATH, [
        _cand("WIN1", now - 3600, passes=True), _cand("WIN2", now - 3600, passes=True),
        _cand("LOSE", now - 3600, passes=False, blocked="score too low")])
    _write(replay.LEARN_OUT, [_practice("WIN1", now - 3000, 4.0), _practice("WIN2", now - 3000, 2.0),
                              _practice("LOSE", now - 3000, -6.0)])
    d = replay.run(30, now=now)
    assert d["saw"]["n"] == 3 and d["policy"]["n"] == 2
    assert d["policy"]["alpha"] == pytest.approx(3.0)
    assert d["saw"]["alpha"] == pytest.approx(0.0)
    assert d["edge_vs_seen"] == pytest.approx(3.0)
    assert d["blocked_by"] == {"score too low": 1}

    # and the inverse: a policy that selects the loser must show a negative edge
    _write(replay.CAND_PATH, [
        _cand("WIN1", now - 3600, passes=False, blocked="x"), _cand("WIN2", now - 3600, passes=False, blocked="x"),
        _cand("LOSE", now - 3600, passes=True)])
    d2 = replay.run(30, now=now)
    assert d2["edge_vs_seen"] < 0, "the harness must be able to report that a policy made things worse"


def test_run_is_safe_on_an_empty_ledger(isolated_state):
    """A fresh volume has no rows yet; the harness reports emptiness rather than raising."""
    d = replay.run(30)
    assert d["saw"]["n"] == 0 and d["policy"]["n"] == 0 and d["edge_vs_seen"] is None
    assert "no labeled candidates" in replay._fmt(d)


# ----------------------------------------------------------------------------------------------
# the Gauntlet — the gate in front of real money. Its FAIL direction is the safety property.
# ----------------------------------------------------------------------------------------------

def _book(n: int, mean_alpha: float, conf: float = 70.0) -> list[dict]:
    """A book with a realistic MIX of winners and losers whose mean alpha is `mean_alpha`.

    A degenerate all-identical-loss ledger scores a legitimate mastery of 0 and would make the
    "the raw number is still reported" assertion vacuous, so the shape here mirrors the live book:
    winners and losers interleaved, net negative or positive by the caller's choice.
    """
    rows = []
    for i in range(n):
        a = (mean_alpha + 3.0) if i % 2 else (mean_alpha - 3.0)   # mean preserved, real spread
        rows.append({"ticker": f"T{i}", "catalyst": "fda_approval", "confidence": conf,
                     "size_usd": 125.0, "entry_price": 100.0, "exit_price": 100 + a,
                     "pnl_pct": a, "pnl_usd": a, "win": a > 0,
                     "entry_ts": time.time() - 86400, "exit_ts": time.time(),
                     "sources": ["fda_approvals"], "alpha": a})
    return rows


@pytest.mark.rail
def test_gauntlet_fails_on_an_unprofitable_book_however_many_trades(isolated_state):
    """The real ledger's shape (129 trades, -0.5%/trade alpha) must FAIL, and say why.

    This is the exact state the bot is in. A gate that certifies it is worthless.
    """
    _write("state/outcomes.jsonl", _book(129, -0.5))
    d = backtest.evaluate(45)
    assert d["passed"] is False
    assert "book_profitable" in d["failed"]
    assert d["overall"]["mastery"] == 0.0, "a failing Gauntlet must not export a passing mastery"
    assert d["raw_mastery"] > 0, "the raw number is still reported for diagnosis"


@pytest.mark.rail
def test_gauntlet_needs_every_check_not_a_good_average(isolated_state):
    """Each check is a veto. A profitable book with no policy evidence still fails."""
    _write("state/outcomes.jsonl", _book(80, 1.5))
    d = backtest.evaluate(45)
    assert d["passed"] is False
    assert "policy_samples" in d["failed"] and "book_samples" not in d["failed"]


@pytest.mark.rail
def test_gauntlet_can_pass_only_on_real_forward_evidence(isolated_state):
    """The PASS path exists and is reachable — but only with a profitable book AND a policy that
    beat the field on enough labeled picks. Proving the gate is not permanently stuck shut."""
    now = time.time()
    _write("state/outcomes.jsonl", _book(80, 1.5))
    # 50 picks the policy TAKES (net positive) + 20 it REJECTS (losers) — so the policy's
    # selection genuinely beats the average of everything it saw, which is what edge measures.
    _write(replay.CAND_PATH,
           [_cand(f"P{i}", now - 3600 - i) for i in range(50)]
           + [_cand(f"R{i}", now - 3600 - i, passes=False, blocked="not graduated") for i in range(20)])
    _write(replay.LEARN_OUT,
           [_practice(f"P{i}", now - 3000 - i, 2.0 if i % 4 else -1.0) for i in range(50)]
           + [_practice(f"R{i}", now - 3000 - i, -4.0) for i in range(20)])
    json.dump({"base_rate": {"n": 100, "ema": 0.36}}, open("state/knowledge.json", "w"))

    d = backtest.evaluate(45, now=now)
    assert d["passed"] is True, f"the gate is stuck shut: {d['failed']}"
    assert d["overall"]["mastery"] == d["raw_mastery"], "a passing gate exports the real mastery"
    assert d["replay"]["edge_vs_seen"] > 0
    assert d["version"].startswith("gauntlet-v3") and d["ts"] > 0
    assert d["stale_after_days"] == backtest.STALE_DAYS


@pytest.mark.rail
def test_gauntlet_never_passes_on_a_replay_error(isolated_state, monkeypatch):
    """If the replay harness raises, the verdict is FAIL — never a silent pass."""
    _write("state/outcomes.jsonl", _book(80, 1.5))

    def boom(*a, **k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(replay, "run", boom)

    d = backtest.evaluate(45)
    assert d["passed"] is False and d["overall"]["mastery"] == 0.0


# ----------------------------------------------------------------------------------------------
# the counterfactual gate — what makes the whole harness possible
# ----------------------------------------------------------------------------------------------

@pytest.mark.invariant
def test_passes_gate_can_be_asked_the_live_question_while_shadow_still_explores(cfg):
    """`enforce_soft=True` answers "would LIVE have taken this?" without changing what shadow does.

    Ten weeks of book trades measured "anything with 2 sources and 2 families" because shadow
    returns True before the graduation gate, min_pwin, the vetoes and the regime bar. Recording the
    live counterfactual is what turns that ledger into evidence about the policy.
    See earshot/brain.py passes_gate and earshot/main.py _record_candidates.
    """
    from earshot.brain import Brain
    from earshot.models import Candidate, Catalyst, RawSignal

    b = Brain(cfg)
    assert b.explore is True, "this build should be exploring in shadow"

    # n_independent is derived from the signals, so build the setup the way the brain does
    sigs = [RawSignal(source=s, weight=1.0, ts=time.time(), tickers=["ZZZZ"], headline="x")
            for s in ("edgar_8k", "newswire", "finnhub_earnings")]
    c = Candidate(ticker="ZZZZ", catalyst=Catalyst.UNKNOWN, confidence=95.0, size_usd=50.0,
                  signals=sigs)
    assert c.n_independent == 3
    c._families = 3
    c.regime_ok = True
    c._analyst_veto = "analyst: not a clean up-catalyst"

    # shadow explores straight through the soft veto...
    ok_shadow, _ = b.passes_gate(c)
    assert ok_shadow is True

    # ...while the same candidate, asked the LIVE question, is refused by that veto.
    ok_live, why_live = b.passes_gate(c, enforce_soft=True)
    assert ok_live is False and why_live
    assert b.explore is True, "asking the counterfactual must not mutate the brain's mode"
