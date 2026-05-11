from __future__ import annotations
"""Pydantic schemas for the recommendation engine.

Kept in its own submodule so legacy `from app.models import Signal` keeps
working via `app/models/__init__.py`. New imports use:
    from app.models.recommendation import Recommendation, SignalContribution
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# Why a separate Literal alias: prevents typos at the producer side and
# documents the closed set for the frontend.
Horizon = Literal["intraday", "swing", "positional"]
Action = Literal["BUY", "SELL", "HOLD", "AVOID"]
MarketCapBand = Literal["LARGE", "MID", "SMALL", "MICRO"]
FiiDiiSignal = Literal["INFLOW", "OUTFLOW", "NEUTRAL"]
FnoSignal = Literal["LONG_BUILDUP", "SHORT_BUILDUP", "LONG_UNWINDING", "SHORT_COVERING"]


class SignalContribution(BaseModel):
    """One factor's contribution to the final conviction score.

    `value` is the raw indicator (e.g. RSI = 62.4). `score` is the normalized
    [-1, +1] signed contribution actually used in the weighted sum.
    """

    name: str
    weight: float = Field(ge=0.0, le=1.0)
    value: Optional[float] = None
    score: float = Field(ge=-1.0, le=1.0)
    direction: Literal["bullish", "bearish", "neutral"]


class Recommendation(BaseModel):
    symbol: str
    exchange: Literal["NSE", "BSE"] = "NSE"
    horizon: Horizon
    action: Action
    conviction: int = Field(ge=0, le=100)
    entry: float
    stoploss: float
    target1: float
    target2: Optional[float] = None
    risk_reward: float = Field(ge=0.0)
    timeframe_days: int = Field(ge=1)
    signals: list[SignalContribution]
    reasons: list[str]
    sector: str
    market_cap_band: MarketCapBand
    last_price: float
    price_change_pct_1d: float
    delivery_pct: Optional[float] = None
    fii_dii_signal: Optional[FiiDiiSignal] = None
    f_and_o_signal: Optional[FnoSignal] = None
    regime: Optional[str] = None
    weighted_score: Optional[float] = None
    factor_agreement: Optional[float] = None
    calibration_note: Optional[str] = None
    data_quality: Optional[str] = None
    portfolio_context: Optional[dict] = None
    advisory_disclaimer: str = (
        "Research signal only, not investment advice. Validate independently "
        "and use your own risk controls."
    )
    generated_at: datetime

    @field_validator("entry", "stoploss", "target1", "last_price")
    @classmethod
    def _positive_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price must be positive")
        return v

    @field_validator("target2")
    @classmethod
    def _t2_positive(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("target2 must be positive")
        return v


class SectorSummary(BaseModel):
    sector: str
    avg_conviction: float
    pick_count: int
    top_picks: list[str]


class RecommendationListResponse(BaseModel):
    """Standard envelope: { data, meta, errors }."""

    data: list[Recommendation]
    meta: dict
    errors: list[dict] = Field(default_factory=list)


class SectorListResponse(BaseModel):
    data: list[SectorSummary]
    meta: dict
    errors: list[dict] = Field(default_factory=list)
