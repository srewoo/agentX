from __future__ import annotations
"""Pydantic request/response models with validation."""
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class Signal(BaseModel):
    id: str
    symbol: str
    signal_type: str
    direction: str
    strength: int = Field(ge=1, le=10)
    reason: str
    risk: Optional[str] = None
    llm_summary: Optional[str] = None
    current_price: Optional[float] = None
    metadata: Optional[dict] = None
    created_at: str
    read: bool = False
    dismissed: bool = False


class WatchlistItem(BaseModel):
    symbol: str
    name: str
    exchange: str = "NSE"


class AddWatchlistRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=100)
    exchange: Literal["NSE", "BSE"] = "NSE"


class AIAnalysisRequest(BaseModel):
    timeframe: Literal["intraday", "swing", "long"] = "swing"


VALID_SIGNAL_TYPES = [
    "price_spike", "volume_spike", "breakout", "rsi_extreme",
    "macd_crossover", "sentiment_shift", "price_alert",
]


class UpdateSettingsRequest(BaseModel):
    alert_interval_minutes: Optional[int] = Field(None, ge=0, le=1440)  # 0 = manual only
    risk_mode: Optional[Literal["conservative", "balanced", "aggressive"]] = None
    signal_types: Optional[list[str]] = None
    llm_provider: Optional[Literal["gemini", "openai", "claude"]] = None
    llm_model: Optional[str] = Field(None, max_length=100)
    llm_api_key: Optional[str] = Field(None, max_length=200)
    openai_api_key: Optional[str] = Field(None, max_length=200)
    gemini_api_key: Optional[str] = Field(None, max_length=200)
    claude_api_key: Optional[str] = Field(None, max_length=200)


class CreateAlertRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    target_price: float = Field(gt=0)
    condition: Literal["above", "below", "pct_change"]
    pct_threshold: Optional[float] = Field(None, ge=0.1, le=50.0)
    note: Optional[str] = Field(None, max_length=500)


class HealthResponse(BaseModel):
    status: str
    db: str
    cache: str
    last_scan: Optional[str]
    market_open: bool


class SignalsResponse(BaseModel):
    signals: list[Signal]
    unread_count: int


class StockQuote(BaseModel):
    symbol: str
    price: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    volume: Optional[float]
    high: Optional[float]
    low: Optional[float]
    open: Optional[float]
    prev_close: Optional[float]
    market_cap: Optional[Any] = None
    name: Optional[str] = None


class TechnicalsResponse(BaseModel):
    symbol: str
    rsi: Optional[float]
    rsi_signal: Optional[str]
    adx: Optional[float]
    macd: Optional[dict]
    moving_averages: Optional[dict]
    bollinger_bands: Optional[dict]
    volume_avg_20: Optional[float]
    price_vs_sma20: Optional[str]
    support_resistance: Optional[dict]
    fibonacci: Optional[dict]
    poc: Optional[float]
    market_regime: Optional[dict]
