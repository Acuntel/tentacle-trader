"""Shared fixtures for the TENTACLE suite.

Two rules every test here obeys:
  1. NEVER touch the real ``state/`` directory. Modules in this package read and write state on
     import and on construction (ladder.json, knowledge.json, seen_*.json), so every test runs
     inside a throwaway cwd with its own empty ``state/``.
  2. NEVER touch the network. Anything that would reach yfinance, SEC, or an LLM is passed in
     explicitly or monkeypatched by the test that needs it.

The suite exists because the autopsy of 2026-09-01 found the exit geometry had silently inverted
(a ~4% target under a 6% stop) with nothing in the repo to catch it — there were zero committed
tests. Each ``invariant`` marker below pins one of those findings so it cannot come back, whether
the change comes from a human or from the bot's own self-coder.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture(scope="session")
def raw_config() -> dict:
    """The REAL config.yaml. Tests assert against shipped values on purpose: a config edit that
    breaks an invariant (e.g. dropping min_reward_risk) must fail the suite, not pass it."""
    with open(REPO / "config.yaml") as f:
        return yaml.safe_load(f)


@pytest.fixture
def cfg(raw_config) -> dict:
    """A deep-ish copy so a test can mutate freely without leaking into the next test."""
    import copy
    return copy.deepcopy(raw_config)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Run every test in a throwaway cwd with an empty state/ + dungeon/.

    Autouse: constructing MotherInstinct writes state/ladder.json, SeenSet writes state/seen_*.json,
    and Learner reads state/knowledge.json. Without this, running the suite would overwrite the
    operator's local ledgers.
    """
    for d in ("state", "state/dungeon", "dungeon"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def no_prices(monkeypatch):
    """Neutralize the price layer: no network, deterministic answers.

    Returns the module so a test can set specific values, e.g.
    ``no_prices.last_price = lambda t, extended=False: 100.0``.
    """
    from earshot import prices
    monkeypatch.setattr(prices, "last_price", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(prices, "market_return_since", lambda *a, **k: 0.0, raising=False)
    monkeypatch.setattr(prices, "forward_return_pct", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(prices, "runup_pct_today", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(prices, "dollar_volume", lambda *a, **k: 1e9, raising=False)
    monkeypatch.setattr(prices, "regime_downtrend", lambda *a, **k: False, raising=False)
    return prices


@pytest.fixture
def no_costs(monkeypatch):
    """Zero friction, so a test can assert on the raw arithmetic of a label."""
    from earshot import costs
    monkeypatch.setattr(costs, "round_trip_pct", lambda *a, **k: 0.0, raising=False)
    return costs


@pytest.fixture
def open_market(monkeypatch):
    """Pretend the regular session is open and every entry timestamp was inside it."""
    from earshot import market
    monkeypatch.setattr(market, "was_closed", lambda ts: False, raising=False)
    monkeypatch.setattr(market, "session", lambda *a, **k: {
        "open": True, "session": "regular", "reason": "regular session",
        "et": "Tue 2026-09-01 10:00 ET", "next_open": "now"}, raising=False)
    return market
