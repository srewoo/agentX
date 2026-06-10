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
    # Layer-2 LLM judge output (None when judging is disabled or failed open).
    llm_verdict: Optional[Literal["keep", "drop", "downgrade"]] = None
    llm_reason: Optional[str] = None
    exchange: Literal["NSE", "BSE"] = "NSE"
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
    llm_judging_enabled: Optional[bool] = None
    # Bull/Bear/Judge adversarial debate over top signals.
    # Costs ~3 LLM calls per debated signal × top-3 signals = max 9 calls/scan.
    debate_enabled: Optional[bool] = None
    # Multi-perspective analyst (#14): 4 specialist LLM agents + synthesiser.
    # Most expensive layer — 5 calls per analysed signal × top-5 = up to 25
    # calls per scan. Off by default.
    multi_perspective_enabled: Optional[bool] = None
    # Advisor + autonomous paper trading toggles — previously frontend-only.
    auto_paper_trade: Optional[bool] = None
    auto_paper_min_strength: Optional[int] = Field(None, ge=1, le=10)
    auto_paper_max_open: Optional[int] = Field(None, ge=1, le=100)
    capital: Optional[float] = Field(None, ge=0)
    risk_per_trade_pct: Optional[float] = Field(None, ge=0, le=100)
    atr_sl_mult: Optional[float] = Field(None, ge=0, le=20)
    atr_target_mult: Optional[float] = Field(None, ge=0, le=20)
    regime_filter: Optional[bool] = None
    roundtrip_cost_pct: Optional[float] = Field(None, ge=0, le=10)
    dedupe_signals: Optional[bool] = None
    audio_alerts: Optional[bool] = None
    audio_strength_threshold: Optional[int] = Field(None, ge=1, le=10)

    # ── Broker integration ────────────────────────────────────────────────
    broker: Optional[Literal["", "angelone", "kite"]] = None
    # AngelOne SmartAPI (all four required to log in).
    angelone_api_key: Optional[str] = Field(None, max_length=200)
    angelone_client_code: Optional[str] = Field(None, max_length=50)
    angelone_mpin: Optional[str] = Field(None, max_length=20)
    angelone_totp_secret: Optional[str] = Field(None, max_length=200)
    # Kite Connect.
    kite_api_key: Optional[str] = Field(None, max_length=200)
    kite_api_secret: Optional[str] = Field(None, max_length=200)
    kite_access_token: Optional[str] = Field(None, max_length=200)
    # ── Upstox data source (authenticated primary) ───────────────────────
    # Daily OAuth access token (expires ~03:30 IST) + app credentials.
    upstox_access_token: Optional[str] = Field(None, max_length=4000)
    upstox_api_key: Optional[str] = Field(None, max_length=200)
    upstox_api_secret: Optional[str] = Field(None, max_length=200)
    # ── Twelve Data keyed fallback ───────────────────────────────────────
    twelvedata_api_key: Optional[str] = Field(None, max_length=200)
    # ── Financial Modeling Prep + Finnhub (fundamentals / earnings / macro) ─
    fmp_api_key: Optional[str] = Field(None, max_length=200)
    finnhub_api_key: Optional[str] = Field(None, max_length=200)


class UpstoxExchangeRequest(BaseModel):
    """Body for the Upstox OAuth code→token exchange."""
    code: str = Field(min_length=1, max_length=4000)
    redirect_uri: str = Field(min_length=1, max_length=2000)


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
