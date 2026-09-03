"""Signal doctrine, tentacle behaviour, and the SAFETY RAILS.

Two kinds of test live here.

RAILS (``@pytest.mark.rail``) pin the rules that keep real money at exactly $0 and keep the hard
risk law outside the model's reach: the executor money gate, selfcode's LOCKED files and
MONEY_GUARD, the CI applier that shares those guards, and risk.py's price/liquidity/no-doubling-up
law. A self-coder that commits to main daily can rewrite almost anything in this repo; these are
the lines it must never be able to cross, so nothing here may ever be weakened or deleted.

INVARIANTS (``@pytest.mark.invariant``) pin findings from the autopsy of 2026-09-01: the
ClinicalTrials "fda" tentacle that was stamped ``catalyst_hint="fda_approval"`` and became the
book's largest catalyst (38 trades including the $450 LLY loss) out of trial-status non-events;
the family doctrine that keeps the crowd from confirming a setup; EDGAR's per-feed isolation and
emit-once memory.

Everything is hermetic: requests/feedparser are stubbed, prices are neutered by the ``no_prices``
fixture, and ``isolated_state`` puts every test in a throwaway cwd. No test here may ever write
into ``earshot/`` — the two selfcode tests assert the target file is byte-identical afterwards.
"""
from __future__ import annotations

import os

import json
import types

import pytest

from earshot import brain, executor, risk, selfcode, apply_patch
from earshot.models import Candidate, Catalyst, RawSignal
from earshot.patterns import PatternMemory
from earshot.sources import base as sbase
from earshot.sources import extra as sextra
from earshot.sources import free as sfree


# --------------------------------------------------------------------------------------- helpers

def sig(source: str, ticker: str = "NVDA", strength: float = 1.0,
        hint: str | None = None, headline: str = "") -> RawSignal:
    """One observation from one tentacle, with an explicit weight so scores are deterministic."""
    return RawSignal(source=source, weight=1.0, ts=0.0, tickers=[ticker],
                     headline=headline or f"{ticker} {source} event", url="",
                     catalyst_hint=hint, strength=strength)


def quiet_brain(cfg: dict) -> brain.Brain:
    """A Brain with the price-action/options/fundamentals layers off (they call yfinance).

    The catalyst/family doctrine under test lives entirely upstream of those multipliers.
    """
    cfg["technicals"]["enabled"] = False
    cfg["options"]["enabled"] = False
    cfg["fundamentals"]["enabled"] = False
    return brain.Brain(cfg)


def by_ticker(cands) -> dict:
    return {c.ticker: c for c in cands}


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, payload=None, text: str = ""):
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


# ===================================================================== 1. THE MONEY GATE (executor)

@pytest.mark.rail
def test_get_executor_always_returns_shadow(no_prices):
    """The only execution backend this build can hand out is the paper one.

    get_executor() ignores the requested mode and returns ShadowExecutor, and a shadow fill moves
    no money: it just books the last price. If a future edit ever routes a live backend through
    here, this test is the thing that screams. earshot/executor.py:93-96, :45-59.
    """
    for mode in ("shadow", "live", "robin_stocks", "robinhood_mcp", "", "LIVE"):
        ex = executor.get_executor(mode)
        assert isinstance(ex, executor.ShadowExecutor), f"get_executor({mode!r}) escaped shadow"
        assert ex.name == "shadow"

    no_prices.last_price = lambda t, extended=False: 12.34
    fill = executor.get_executor("live").buy(
        Candidate(ticker="NVDA", catalyst=Catalyst.UNKNOWN, confidence=90.0, size_usd=25.0))
    assert fill["status"] == "shadow_fill"
    assert fill["price"] == 12.34 and fill["usd"] == 25.0


@pytest.mark.rail
def test_live_is_refused_at_rung_zero_and_unarmed(isolated_state):
    """Real capital needs THREE independent unlocks; any one missing refuses.

    live_allowed() must refuse when (a) the Gauntlet has not been passed, (b) the ladder is still
    on a $0 rung, or (c) no human has armed it — and the live executors must raise rather than
    reach robin_stocks. earshot/executor.py:21-35, :68-90.
    """
    # (a) no gauntlet file at all -> refused, whatever the rung/arm say.
    ok, why = executor.live_allowed(4, True)
    assert ok is False and "gauntlet" in why.lower()

    # Now pass the Gauntlet in this throwaway cwd; the other two locks must still hold.
    (isolated_state / "state" / "gauntlet.json").write_text(
        json.dumps({"overall": {"mastery": executor.GAUNTLET_PASS_MASTERY + 5}}))
    assert executor.live_allowed(0, True) == (False, "still on a $0 rung (shadow) — no live capital unlocked")
    assert executor.live_allowed(-1, True)[0] is False
    ok, why = executor.live_allowed(1, False)
    assert ok is False and "armed" in why

    # Only all three together permit it — and that state is NOT what this build ships.
    assert executor.live_allowed(1, True)[0] is True

    # The live backends refuse loudly at rung 0 / unarmed, before any broker import.
    c = Candidate(ticker="NVDA", catalyst=Catalyst.UNKNOWN, confidence=99.0, size_usd=500.0)
    for backend in (executor.RobinStocksExecutor(), executor.RobinhoodMCPExecutor()):
        with pytest.raises(RuntimeError, match="LIVE refused"):
            backend.buy(c, rung=0, armed=False)


# ===================================================== 2/3. SELFCODE + CI APPLIER: LOCKED + GUARD

@pytest.mark.rail
def test_locked_rails_are_the_money_gate_and_the_guard_itself():
    """The locked set must contain the money rails AND the self-modification machinery.

    Losing risk.py/executor.py/backtest.py would let the coder rewrite the risk law or the
    executor; losing selfcode.py/apply_patch.py would let it delete this guard.
    earshot/selfcode.py:49.
    """
    assert {"risk.py", "executor.py", "backtest.py"} <= selfcode.LOCKED
    assert {"selfcode.py", "apply_patch.py"} <= selfcode.LOCKED


@pytest.mark.rail
@pytest.mark.parametrize("rail", ["risk.py", "executor.py", "backtest.py"])
def test_apply_external_refuses_a_patch_to_a_locked_rail(monkeypatch, repo_root, rail):
    """An approved patch aimed at a locked rail is refused and the file is not touched.

    This is the rule that keeps $0 real: the autonomous coder proposes, a human taps approve, and
    the applier STILL refuses to edit the risk law or the executor. earshot/selfcode.py:542-544.
    """
    monkeypatch.setattr(selfcode, "_fitness", lambda: None)   # no subprocess; guards run first anyway
    target = repo_root / "earshot" / rail
    before = target.read_bytes()

    try:
        res = selfcode.apply_external([{"file": f"earshot/{rail}",
                                        "find": "def ", "replace": "def  "}])
    finally:
        touched = target.read_bytes() != before
        if touched:                            # belt and braces: never leave the tree edited
            target.write_bytes(before)

    assert res["status"] == "refused"
    assert "locked rail" in res["why"]
    assert touched is False, f"{rail} was modified despite the refusal"


@pytest.mark.rail
@pytest.mark.parametrize("poison", [
    "    return rh.order_buy_fractional_by_price(c.ticker, c.size_usd)",
    "        self.armed = True",
    "    ex = RobinStocksExecutor()",
    "    ok, why = live_allowed(9, armed=True)",
    "    broker.place_order(ticker, usd)",
])
def test_money_guard_refuses_unlock_tokens_in_any_file(monkeypatch, repo_root, poison):
    """A patch to ANY file is refused if its new code introduces a money-unlock token.

    LOCKED protects five filenames; MONEY_GUARD protects the concept — a patch to a harmless
    module (notify.py here) that smuggles in order_buy / armed=True / RobinStocksExecutor /
    live_allowed / place_order is refused outright, so full autonomy can never reach real money.
    earshot/selfcode.py:52-59, :576-578.
    """
    monkeypatch.setattr(selfcode, "_fitness", lambda: None)
    target = repo_root / "earshot" / "notify.py"
    src = target.read_text()
    before = target.read_bytes()
    # a genuinely unique anchor, so the refusal is the money guard and not "find not unique"
    anchor = next(ln for ln in src.splitlines()
                  if len(ln.strip()) > 25 and src.count(ln) == 1 and "\\" not in ln)

    try:
        res = selfcode.apply_external([{"file": "earshot/notify.py",
                                        "find": anchor, "replace": anchor + "\n" + poison}])
    finally:
        touched = target.read_bytes() != before
        if touched:
            target.write_bytes(before)

    assert res["status"] == "refused"
    assert "money/safety gate" in res["why"], "refused for the wrong reason (not the money guard)"
    assert touched is False, "notify.py was modified despite the refusal"

    # and the guard helper agrees on the token, while ordinary code passes freely
    assert selfcode._unsafe(poison) is True
    assert selfcode._unsafe(anchor) is False


@pytest.mark.rail
def test_ci_applier_shares_the_same_guards(monkeypatch, capsys, repo_root):
    """apply_patch.py (the GitHub-Action entry point) refuses exactly what selfcode refuses.

    The approved-patch path runs in CI, not in the trader, so it needs its own proof: a locked
    rail and a money-unlock token must both be refused there too, and nothing may be written.
    earshot/apply_patch.py:15-31 -> earshot/selfcode.py:546-580.
    """
    monkeypatch.setattr(selfcode, "_fitness", lambda: None)
    risk_before = (repo_root / "earshot" / "risk.py").read_bytes()
    notify = repo_root / "earshot" / "notify.py"
    notify_before = notify.read_bytes()
    anchor = next(ln for ln in notify.read_text().splitlines()
                  if len(ln.strip()) > 25 and notify.read_text().count(ln) == 1 and "\\" not in ln)

    try:
        monkeypatch.setenv("PATCHES_JSON", json.dumps(
            [{"file": "earshot/risk.py", "find": "def check", "replace": "def  check"}]))
        assert apply_patch.main() == 0
        locked_out = json.loads(capsys.readouterr().out)

        # client_payload double-encodes the list; the money guard must survive that path too.
        monkeypatch.setenv("PATCHES_JSON", json.dumps(json.dumps(
            [{"file": "earshot/notify.py", "find": anchor,
              "replace": anchor + "\n    account_number = '469628218'"}])))
        assert apply_patch.main() == 0
        guard_out = json.loads(capsys.readouterr().out)
    finally:
        touched = [p.name for p, b in ((repo_root / "earshot" / "risk.py", risk_before),
                                       (notify, notify_before)) if p.read_bytes() != b]
        for p, b in ((repo_root / "earshot" / "risk.py", risk_before), (notify, notify_before)):
            if p.read_bytes() != b:
                p.write_bytes(b)

    assert locked_out["status"] == "refused" and "locked rail" in locked_out["why"]
    assert guard_out["status"] == "refused" and "money/safety gate" in guard_out["why"]
    assert touched == [], f"the CI applier wrote to {touched} despite refusing"


@pytest.mark.rail
def test_apply_external_refuses_paths_outside_the_package(monkeypatch):
    """Only earshot/*.py and config.yaml are patchable — not workflows, not scripts, not tests.

    A patch that could rewrite .github/workflows or this very test suite would route around every
    other rail in this file. earshot/selfcode.py:540-549.
    """
    monkeypatch.setattr(selfcode, "_fitness", lambda: None)
    for path in (".github/workflows/deploy.yml", "tests/test_sources_and_rails.py",
                 "scripts/run.sh", "../outside.py", "requirements.txt"):
        res = selfcode.apply_external([{"file": path, "find": "a", "replace": "b"}])
        assert res["status"] == "refused", f"{path} was not refused"
        assert "not allowed" in res["why"] or "locked rail" in res["why"]


# ============================================================================ 4. RISK.PY HARD LAW

@pytest.mark.rail
def test_risk_law_blocks_pennies_illiquids_and_doubling_up(cfg, no_prices, open_market):
    """The hard risk law is enforced OUTSIDE the model: penny, illiquid, already-held are refused.

    These floors also live in config.yaml, which IS patchable by the approved-patch path, so the
    magnitudes are pinned here too — a "tuning" patch that sets min_price to 0 must fail this
    suite, not ship. earshot/risk.py:18-48, config.yaml risk.min_price / risk.min_dollar_volume.
    """
    r = cfg["risk"]
    assert r["min_price"] >= 1.0, "penny filter gutted in config.yaml"
    assert r["min_dollar_volume"] >= 1_000_000, "liquidity floor gutted in config.yaml"

    gate = risk.RiskGate(cfg)
    instinct = types.SimpleNamespace(dead=False)
    c = Candidate(ticker="NVDA", catalyst=Catalyst.EARNINGS_BEAT, confidence=99.0, size_usd=25.0,
                  signals=[sig("edgar_8k"), sig("finnhub_earnings")])

    # control: healthy name, empty book -> cleared
    no_prices.last_price = lambda *a, **k: 50.0
    no_prices.dollar_volume = lambda *a, **k: 1e9
    assert gate.check(c, instinct, [], 0.0) == (True, "cleared hard risk law")

    # below min_price -> refused (pump/penny filter)
    no_prices.last_price = lambda *a, **k: r["min_price"] - 0.01
    ok, why = gate.check(c, instinct, [], 0.0)
    assert ok is False and "below min" in why

    # below the liquidity floor -> refused
    no_prices.last_price = lambda *a, **k: 50.0
    no_prices.dollar_volume = lambda *a, **k: r["min_dollar_volume"] - 1
    ok, why = gate.check(c, instinct, [], 0.0)
    assert ok is False and "liquidity floor" in why

    # already holding the name -> refused, no doubling up on one ticker
    no_prices.dollar_volume = lambda *a, **k: 1e9
    ok, why = gate.check(c, instinct, [{"ticker": "NVDA"}], 0.0)
    assert ok is False and "already holding NVDA" in why
    # ...but a DIFFERENT name in the same book is fine
    assert gate.check(c, instinct, [{"ticker": "AMD"}], 0.0)[0] is True


# ================================================ 5/8. FAMILY DOCTRINE: fda, triggers, confirmers

@pytest.mark.invariant
def test_clinicaltrials_family_is_confirm_only_not_a_trigger():
    """"fda" (ClinicalTrials trial-status rows) is a CONFIRM family; "fda_approvals" is the trigger.

    AUTOPSY 2026-09-01: a trial finishing enrollment was mapped to a regulatory TRIGGER, which
    manufactured the book's biggest catalyst out of non-events — 38 trades including the $450 LLY
    loss. The real approval event is openFDA. earshot/brain.py:30-35, :50-51.
    """
    assert brain.FAMILY["fda"] == "news"
    assert brain.FAMILY["fda"] not in brain.TRIGGER_FAMILIES
    assert brain.FAMILY["fda_approvals"] == "regulatory"
    assert brain.FAMILY["fda_approvals"] in brain.TRIGGER_FAMILIES
    # the crowd never triggers either, and the two doctrines never overlap
    assert brain.ANTI_FAMILIES == {"social", "crowd"}
    assert not (brain.TRIGGER_FAMILIES & brain.ANTI_FAMILIES)


@pytest.mark.invariant
def test_trial_status_alone_cannot_qualify_a_name(cfg, no_prices):
    """Two confirm-only sources never make a conviction setup; one openFDA trigger does.

    Behavioural proof of the family map above, through Brain.evaluate: "fda"+news is one family
    and no trigger, "fda"+attention is two families but still no trigger, and only the openFDA
    approval (regulatory) unlocks qualification. earshot/brain.py:202-207.
    """
    b = quiet_brain(cfg)
    signals = [
        sig("fda", "LLY", strength=0.4), sig("google_news", "LLY"),          # 1 family, no trigger
        sig("fda", "MRNA", strength=0.4), sig("wiki_attention", "MRNA"),     # 2 families, no trigger
        sig("fda_approvals", "PFE"), sig("google_news", "PFE"),              # regulatory trigger
    ]
    got = by_ticker(b.evaluate(signals, capital=500.0, regime_down=False))

    assert got["LLY"]._qualified is False
    assert got["MRNA"]._qualified is False
    assert got["PFE"]._qualified is True


@pytest.mark.invariant
def test_a_trigger_family_is_required_for_conviction(cfg, no_prices):
    """News + attention is two families but zero triggers, so the name does not qualify.

    Doctrine: fast news is a race we structurally lose; confirm families corroborate a
    slow-diffusion trigger (filing/regulatory/surprise/demand/flow/alt) but can never anchor a
    setup. Swapping one confirmer for a filing flips it. earshot/brain.py:43-51, :202-207.
    """
    b = quiet_brain(cfg)
    signals = [
        sig("google_news", "AAPL"), sig("wiki_attention", "AAPL"),   # news + attention, no trigger
        sig("google_news", "MSFT"), sig("edgar_8k", "MSFT"),         # news + FILING -> trigger
    ]
    got = by_ticker(b.evaluate(signals, capital=500.0, regime_down=False))

    assert got["AAPL"]._families == 2, "both confirm families were counted"
    assert got["AAPL"]._qualified is False, "a trigger-less name must not reach conviction"
    assert got["MSFT"]._qualified is True


# ============================================================== 7. ANTI FAMILIES (the crowd tax)

@pytest.mark.invariant
def test_social_only_setup_never_qualifies(cfg, no_prices):
    """A name whose only families are social/crowd is not a setup at all.

    ANTI families measure COMPETITION, not information: they are stripped from the confirmation
    count entirely, so two social sources leave zero families. earshot/brain.py:202-207.
    """
    b = quiet_brain(cfg)
    signals = [sig("reddit", "GME"), sig("stocktwits", "GME"), sig("polymarket", "GME")]
    c = by_ticker(b.evaluate(signals, capital=500.0, regime_down=False))["GME"]

    assert c.n_independent == 3, "three distinct tentacles did fire"
    assert c._families == 0, "social/crowd must not count toward the family count"
    assert c._qualified is False


@pytest.mark.invariant
def test_crowd_presence_penalizes_instead_of_confirming(cfg, no_prices):
    """Adding a social source LOWERS the score; adding a real source of an existing family does not.

    The two names below are matched on everything the scorer sees — same source count, same
    strengths, same family count — so the only difference is the crowding penalty. Every
    learner-vetoed pattern contains +social: when the crowd confirms, we are late.
    earshot/brain.py:222-224.
    """
    b = quiet_brain(cfg)
    signals = [
        # filing + surprise + a THIRD filing source: 3 sources, 2 families, no crowd
        sig("edgar_8k", "CLEAN"), sig("finnhub_earnings", "CLEAN"), sig("sec_fulltext", "CLEAN"),
        # filing + surprise + apewisdom: 3 sources, still 2 families, but the crowd is on it
        sig("edgar_8k", "CROWD"), sig("finnhub_earnings", "CROWD"), sig("apewisdom", "CROWD"),
    ]
    got = by_ticker(b.evaluate(signals, capital=500.0, regime_down=False))
    clean, crowd = got["CLEAN"], got["CROWD"]

    assert clean.n_independent == crowd.n_independent == 3
    assert clean._families == crowd._families == 2, "the social source added no family"
    assert crowd.confidence < clean.confidence, "crowding must cost conviction, not add it"
    assert crowd._qualified is True and clean._qualified is True, "both still have a filing trigger"
    assert any("crowding" in r for r in crowd.reasons)
    assert not any("crowding" in r for r in clean.reasons)


# ==================================================================== 12. passes_gate HARD CHECKS

def _gate_candidate(**over) -> Candidate:
    c = Candidate(ticker="NVDA", catalyst=Catalyst.EARNINGS_BEAT, confidence=90.0, size_usd=25.0,
                  signals=[sig("edgar_8k"), sig("finnhub_earnings")])
    c._families = 2
    c._signature = "earnings_beat|filing+surprise"
    for k, v in over.items():
        setattr(c, k, v)
    return c


@pytest.mark.rail
def test_passes_gate_hard_checks_bite_even_in_shadow_explore(cfg, no_prices):
    """min sources, min families, min score and the pattern-loser veto are enforced ALWAYS.

    Shadow-explore deliberately bypasses the soft/heuristic vetoes so the learner gathers labels;
    it must never bypass the hard checks, which are structural (it is not a real setup) or
    data-backed (this signature keeps losing). earshot/brain.py:500-518.
    """
    b = quiet_brain(cfg)
    assert b.explore is True, "this config is shadow-explore; the hard checks must still bite"
    conv = cfg["conviction"]

    # control: a clean setup passes, and says so as a shadow-explore pass
    ok, why = b.passes_gate(_gate_candidate())
    assert ok is True and "shadow-explore" in why

    # too few independent sources
    thin = _gate_candidate(signals=[sig("edgar_8k")])
    ok, why = b.passes_gate(thin)
    assert ok is False and f"need {conv['min_independent_sources']}" in why

    # enough sources, only one family
    ok, why = b.passes_gate(_gate_candidate(_families=1))
    assert ok is False and "signal family" in why

    # below the score floor
    ok, why = b.passes_gate(_gate_candidate(confidence=conv["min_score_to_trade"] - 0.1))
    assert ok is False and "<" in why

    # a signature the ledger has proven loses: 0 wins in 30 forward outcomes
    know = {"patterns": {"earnings_beat|filing+surprise":
                         {"trades": 30, "wins": 0, "sum_ret": -90.0, "sum_win_ret": 0.0}}}
    b.patterns = PatternMemory(know)
    ok, why = b.passes_gate(_gate_candidate())
    assert ok is False and "persistent loser" in why


# ======================================================= 6. ClinicalTrialsSource emits unhinted

CT_PAYLOAD = {"studies": [
    {"protocolSection": {
        "identificationModule": {"briefTitle": "A Study of Tirzepatide in Adults With Obesity"},
        "statusModule": {"overallStatus": "COMPLETED"},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Eli Lilly and Company"}}}},
    {"protocolSection": {
        "identificationModule": {"briefTitle": "Still enrolling"},
        "statusModule": {"overallStatus": "RECRUITING"},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Pfizer Inc."}}}},
]}


@pytest.mark.invariant
def test_clinicaltrials_emits_unhinted_low_strength(monkeypatch):
    """A trial-status row must carry NO catalyst hint and low strength — it is not an approval.

    AUTOPSY 2026-09-01: this row used to be stamped catalyst_hint="fda_approval", which made
    ClinicalTrials the book's largest catalyst (38 trades, incl. the $450 LLY loss) on non-events.
    Unhinted means classify() falls back to keywords, and "trial COMPLETED" is not an approval
    phrase, so it lands as UNKNOWN. earshot/sources/extra.py:128-155.
    """
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(CT_PAYLOAD)

    monkeypatch.setattr(sextra, "requests", types.SimpleNamespace(get=fake_get))
    src = sextra.ClinicalTrialsSource({"fda": 0.5})
    out = src.fetch()

    assert len(calls) == 1 and "clinicaltrials.gov" in calls[0]
    assert len(out) == 1, "only COMPLETED / ACTIVE_NOT_RECRUITING rows emit"
    s = out[0]
    assert s.source == "fda"
    assert s.tickers == ["LLY"]
    assert s.catalyst_hint is None, "a trial status must NEVER be hinted as an fda_approval"
    assert s.strength == 0.4 and s.strength < 1.0, "confirm-only strength"
    # end to end: unhinted + non-approval wording -> not the FDA_APPROVAL catalyst
    assert brain.classify(s) is Catalyst.UNKNOWN
    assert brain.FAMILY[s.source] not in brain.TRIGGER_FAMILIES


# ================================================================ 9. SeenSet: emit once, forever

@pytest.mark.invariant
def test_seenset_is_emit_once_and_survives_a_restart(isolated_state):
    """A discrete event enters the brain ONCE, and a restart does not re-emit history.

    AUTOPSY 2026-09-01: sources without a seen-set re-emitted every filing on every poll with a
    fresh timestamp — defeating the buffer dedup, drum-beating one 8-K into all-day fake
    confirmation, and teaching the lead-time learner polling cadence instead of who fired first.
    earshot/sources/base.py:56-92.
    """
    s = sbase.SeenSet("unit")
    assert s.seen("acc-1") is False
    s.add("acc-1")
    assert s.seen("acc-1") is True
    s.add("acc-1")                                   # idempotent: no duplicate row
    assert s._items.count("acc-1") == 1

    path = isolated_state / "state" / "seen_unit.json"
    assert path.exists(), "the seen-set must persist on the volume, not in memory"
    assert json.loads(path.read_text()) == ["acc-1"]

    reborn = sbase.SeenSet("unit")                   # a restart / redeploy
    assert reborn.seen("acc-1") is True, "a restart must not re-emit yesterday's filings"
    assert reborn.seen("acc-2") is False

    # a different tentacle keeps its own memory
    assert sbase.SeenSet("other").seen("acc-1") is False


@pytest.mark.invariant
def test_seenset_fifo_cap_forgets_oldest_first(isolated_state):
    """The cap is FIFO and bounded: the newest keys stay remembered, the oldest fall off.

    A cap that dropped the NEWEST keys would silently restore the re-emission bug it exists to
    prevent. earshot/sources/base.py:83-86.
    """
    s = sbase.SeenSet("capped", cap=3)
    for k in ("a", "b", "c", "d"):
        s.add(k)
    assert s.seen("a") is False, "oldest key must be the one evicted"
    assert [s.seen(k) for k in ("b", "c", "d")] == [True, True, True]
    assert len(s._items) == 3


# ============================================================ 10. EDGAR per-feed isolation

@pytest.mark.invariant
def test_edgar_feeds_are_isolated_one_bad_feed_costs_only_that_feed(monkeypatch, isolated_state):
    """A single failing EDGAR feed must not take the other two down with it.

    AUTOPSY 2026-09-01: one timeout dropped 8-K + Form 4 + 13D together for the whole poll — the
    bot's top-trust primary source went dark on one slow response. Here the Form 4 feed raises and
    the 8-K and 13D signals must still come through, with errors incremented exactly once.
    earshot/sources/free.py:117-134.
    """
    def fake_get(url, **kw):
        if "company_tickers" in url:
            return _Resp({})                      # empty CIK map -> ticker comes from the title
        if "type=4&" in url:
            raise TimeoutError("read timed out")  # ONLY the Form 4 feed is sick
        form = "8-K" if "type=8-K" in url else "13D"
        return _Resp(text=form)

    def fake_parse(text):
        return types.SimpleNamespace(entries=[types.SimpleNamespace(
            title=f"{text} - NVIDIA CORP $NVDA (0001045810) (Filer)",
            link=f"https://sec.gov/{text}/0001045810-26-000001-index.htm")])

    monkeypatch.setattr(sfree, "requests", types.SimpleNamespace(get=fake_get))
    monkeypatch.setattr(sfree, "feedparser", types.SimpleNamespace(parse=fake_parse))

    src = sfree.EdgarSource({"edgar_8k": 1.0, "edgar_form4": 1.0, "edgar_13d": 1.0}, "tentacle test")
    out = src.fetch()

    assert {s.source for s in out} == {"edgar_8k", "edgar_13d"}, \
        "one sick feed sank the healthy ones"
    assert src.errors == 1, "exactly the failing feed was counted"
    assert all(s.tickers == ["NVDA"] for s in out)
    # doctrine that rides along: raw Form 4s / 8-Ks are unhinted, only 13D is an activist stake
    hints = {s.source: s.catalyst_hint for s in out}
    assert hints["edgar_8k"] is None
    assert hints["edgar_13d"] == "activist_stake"
    assert src.HINTS["edgar_form4"] is None, "the Form 4 firehose must not be hinted insider_cluster"

    # second poll: same feed contents, nothing re-emitted (emit-once via the seen set)
    assert src.fetch() == []


# ======================================================= 11. safe_fetch: one bad tentacle is fine

@pytest.mark.rail
def test_safe_fetch_swallows_the_exception_and_counts_it():
    """A tentacle that throws returns [] and bumps its error count — it never sinks the cycle.

    main() iterates every source each cycle; an unhandled fetch error would abort the whole poll,
    so the whole octopus would go dark because one arm did. earshot/sources/base.py:115-122.
    """
    class Boom(sbase.Source):
        key = "boom"

        def fetch(self):
            raise RuntimeError("upstream 503")

    class Fine(sbase.Source):
        key = "fine"

        def fetch(self):
            return [sig("fine")]

    boom, fine = Boom({}), Fine({})
    assert boom.safe_fetch() == []
    assert boom.errors == 1
    assert boom.safe_fetch() == [] and boom.errors == 2, "each failure is counted"
    assert boom._last > 0, "a failed poll still consumes its rate-limit slot"
    assert len(fine.safe_fetch()) == 1, "a healthy tentacle is unaffected"

    # a source returning None is normalized to a list, not propagated as None
    class Empty(sbase.Source):
        key = "empty"

        def fetch(self):
            return None

    assert Empty({}).safe_fetch() == []


# ----------------------------------------------------------------------------------------------
# RAIL — the config envelope. risk.py is locked; the numbers it enforces live in config.yaml.
# ----------------------------------------------------------------------------------------------

@pytest.mark.rail
def test_config_rail_refuses_patches_that_loosen_the_risk_law(tmp_path, monkeypatch):
    """An approved config patch may never touch the safety envelope or the sizing law.

    Found writing this suite (2026-09-02): apply_external explicitly allows `rel == "config.yaml"`,
    LOCKED only covers .py rails, and MONEY_GUARD only matches money-unlock CODE tokens. So the
    risk law was locked in code and wide open in data — a patch could set `min_price: 0.0`,
    `daily_loss_pct: 0.9`, empty the ladder, or flip `mode: live`, and nothing would object.
    See earshot/selfcode.py CONFIG_GUARD / _unsafe_config and the config branch of apply_external.
    """
    from earshot import selfcode

    # every protected key is refused, on the REPLACE side...
    for line, key in [("  min_price: 0.0", "min_price"),
                      ("  daily_loss_pct: 0.9", "daily_loss_pct"),
                      ("  mode: live", "mode"),
                      ("  max_position_frac: 1.0", "max_position_frac"),
                      ("  min_reward_risk: 0.1", "min_reward_risk"),
                      ("  min_families: 1", "min_families"),
                      ("  allow_short: true", "allow_short"),
                      ("  starting_capital: 100000", "starting_capital"),
                      ("  ruin_floor_pct: 0.99", "ruin_floor_pct")]:
        assert selfcode._unsafe_config(line) == key, f"{key} is not guarded"

    # ...and on the FIND side too, so a patch cannot DELETE a guard line it may not rewrite
    assert selfcode._unsafe_config("  min_dollar_volume: 5000000") == "min_dollar_volume"

    # ordinary tuning knobs stay editable — the rail must not freeze the whole file
    for benign in ["  family_bonus: 0.2", "  top_k: 40", "  poll_seconds: 15",
                   "  trail_pct: 6.0", "  observe_cap: 3000"]:
        assert selfcode._unsafe_config(benign) is None, f"{benign!r} should stay tunable"


@pytest.mark.rail
def test_config_rail_blocks_the_patch_before_it_touches_the_file():
    """apply_external must REFUSE a guarded config patch and leave config.yaml byte-identical."""
    from earshot import selfcode

    cfg_path = os.path.join(os.path.dirname(selfcode.PKG), "config.yaml")
    before = open(cfg_path, "rb").read()
    try:
        res = selfcode.apply_external([{
            "file": "config.yaml",
            "find": "  min_price: 2.0",
            "replace": "  min_price: 0.0",
            "why": "test: attempt to disable the penny-stock floor"}])
        assert res["status"] == "refused", f"a risk-law patch was not refused: {res}"
        assert "min_price" in res["why"]
    finally:
        after = open(cfg_path, "rb").read()
        if after != before:                       # belt and braces — never leave the repo edited
            open(cfg_path, "wb").write(before)
    assert after == before, "config.yaml was modified by a patch that should have been refused"
