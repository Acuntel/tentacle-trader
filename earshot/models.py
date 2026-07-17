"""Shared data model. Every module agrees on these shapes."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


def now_ts() -> float:
    return time.time()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Catalyst(str, Enum):
    EARNINGS_BEAT = "earnings_beat"
    INDEX_INCLUSION = "index_inclusion"
    FDA_APPROVAL = "fda_approval"
    MNA_TARGET = "mna_target"
    GOV_CONTRACT = "gov_contract"
    ACTIVIST_STAKE = "activist_stake"
    INSIDER_CLUSTER = "insider_cluster"
    AI_QUANTUM_BREAKOUT = "ai_quantum_breakout"   # retired hint (kept for stored patterns)
    DEMAND_SURGE = "demand_surge"                 # alt-data usage/adoption surge — thesis hold
    UNKNOWN = "unknown"


@dataclass
class RawSignal:
    """One observation from one tentacle."""
    source: str                      # source key, e.g. "edgar_8k"
    weight: float                    # current trust weight of this source
    ts: float                        # when observed (epoch)
    tickers: list[str] = field(default_factory=list)
    headline: str = ""
    url: str = ""
    catalyst_hint: Optional[str] = None
    strength: float = 1.0            # 0..2 how strong this single observation is
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts_iso"] = iso(self.ts)
        return d


@dataclass
class Candidate:
    """A ticker that >= N independent tentacles agree on, scored and (maybe) sized."""
    ticker: str
    catalyst: Catalyst
    confidence: float                # 0..100
    size_usd: float                  # what the bot WOULD deploy at its current rung
    reasons: list[str] = field(default_factory=list)
    signals: list[RawSignal] = field(default_factory=list)
    runup_pct: Optional[float] = None
    regime_ok: bool = True
    stop_pct: float = 8.0
    target_pct: float = 12.0
    hold_days: int = 5
    created_ts: float = field(default_factory=now_ts)

    @property
    def n_independent(self) -> int:
        return len({s.source for s in self.signals})

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "catalyst": self.catalyst.value,
            "confidence": round(self.confidence, 1),
            "size_usd": round(self.size_usd, 2),
            "n_sources": self.n_independent,
            "runup_pct": self.runup_pct,
            "regime_ok": self.regime_ok,
            "reasons": self.reasons,
            "sources": sorted({s.source for s in self.signals}),
            "created": iso(self.created_ts),
        }


@dataclass
class Outcome:
    ticker: str
    catalyst: str
    confidence: float
    size_usd: float
    entry_price: float
    exit_price: float
    pnl_pct: float
    pnl_usd: float
    win: bool
    entry_ts: float
    exit_ts: float
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_iso"] = iso(self.entry_ts)
        d["exit_iso"] = iso(self.exit_ts)
        return d
