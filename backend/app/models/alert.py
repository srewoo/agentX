"""Pydantic models for the multi-channel alert / notification subsystem.

These types model the contract between the alerts API, the routing service
(`services.notifications`), and the per-channel adapters in
`services.channels.*`. They're deliberately separate from the legacy
`price_alerts` table used by `services.alert_checker` so we can evolve
condition kinds and channel fan-out independently.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Channel identifiers. Kept as a Literal so request bodies fail fast on typos.
ChannelName = Literal["telegram", "email", "whatsapp", "sms", "push"]

# Supported condition kinds. New kinds: add here + handle in
# `services.notifications.evaluate_condition`.
ConditionKind = Literal[
    "price_above",
    "price_below",
    "pct_change_1d_above",
    "pct_change_1d_below",
    "recommendation_conviction_above",
    "volume_spike_above",
    "breakout",
]

# Default user_id when none is supplied. Single-tenant deployments keep
# all alerts scoped to this synthetic user — multi-tenant deploys should
# set it from the auth context.
DEFAULT_USER_ID = "local"


class AlertCondition(BaseModel):
    """The evaluable predicate of an Alert.

    `payload` holds kind-specific parameters, e.g.:
      - price_above:                    {"price": 1234.5}
      - pct_change_1d_above:            {"pct": 5.0}
      - recommendation_conviction_above:{"score": 7}
      - volume_spike_above:             {"ratio": 2.5}
      - breakout:                       {"lookback_days": 20}
    Validation per kind happens at evaluation time so we don't have to
    fan out into N pydantic subtypes for a feature that's still evolving.
    """

    kind: ConditionKind
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateAlertRequest(BaseModel):
    """Body for `POST /api/alerts`."""

    symbol: str = Field(min_length=1, max_length=20)
    condition: AlertCondition
    channels: list[ChannelName] = Field(min_length=1)
    note: Optional[str] = Field(None, max_length=500)
    active: bool = True

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be empty")
        return v

    @field_validator("channels")
    @classmethod
    def _dedupe_channels(cls, v: list[str]) -> list[str]:
        # Order-preserving dedupe — we want fan-out to hit each channel once.
        seen: set[str] = set()
        out: list[str] = []
        for c in v:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out  # type: ignore[return-value]


class UpdateAlertRequest(BaseModel):
    """Body for `PATCH /api/alerts/{id}`. All fields optional."""

    active: Optional[bool] = None
    channels: Optional[list[ChannelName]] = None
    note: Optional[str] = Field(None, max_length=500)


class TestNotificationRequest(BaseModel):
    """Body for `POST /api/alerts/test`."""

    channels: list[ChannelName] = Field(min_length=1)
    message: str = Field(min_length=1, max_length=1000, default="agentX test notification")
    symbol: Optional[str] = Field(None, max_length=20)


class Alert(BaseModel):
    """Stored alert row."""

    id: str
    user_id: str = DEFAULT_USER_ID
    symbol: str
    condition: AlertCondition
    channels: list[ChannelName]
    active: bool = True
    note: Optional[str] = None
    created_at: str

    @classmethod
    def now_iso(cls) -> str:
        return datetime.now(timezone.utc).isoformat()


class NotificationMessage(BaseModel):
    """The thing a channel adapter actually sends.

    Channel adapters MUST treat this as immutable input — no mutation,
    no enrichment. If a channel needs extra context, add it here.
    """

    alert_id: Optional[str] = None
    user_id: str = DEFAULT_USER_ID
    symbol: Optional[str] = None
    title: str
    body: str
    # Free-form metadata for things like deep-links, priorities, idempotency.
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryResult(BaseModel):
    """Outcome of a single channel send. Channels never raise to callers —
    they translate exceptions into a DeliveryResult with `ok=False`.
    """

    channel: str
    ok: bool
    status: Literal["delivered", "skipped", "failed", "throttled", "deduped"]
    error: Optional[str] = None
    # Provider-specific id (Telegram message_id, Twilio sid, SMTP message-id).
    provider_id: Optional[str] = None
    attempts: int = 1
    duration_ms: Optional[int] = None
