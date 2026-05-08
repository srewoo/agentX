"""Alerts API.

This router serves two contracts in one URL space:

  1. Legacy single-channel price-alert shape (back-compat with existing
     extension + tests):
        POST  /api/alerts        body: {symbol, target_price, condition}
        GET   /api/alerts                     -> {alerts: [...]}
        GET   /api/alerts/history             -> {alerts: [...]}
        DELETE /api/alerts/{id}

  2. New multi-channel notification-center shape (this PR):
        POST   /api/alerts            body: {symbol, condition: {...}, channels: [...]}
        GET    /api/alerts/v2                  -> {alerts: [...]}    (always new shape)
        GET    /api/alerts/{id}                -> {alert: ...}
        PATCH  /api/alerts/{id}                -> toggle active / channels
        DELETE /api/alerts/{id}
        POST   /api/alerts/test                -> per-channel send result
        GET    /api/alerts/config              -> non-secret provider config
        POST   /api/alerts/config              -> upsert provider config
        GET    /api/alerts/{id}/events         -> recent fire events
        GET    /api/alerts/log                 -> recent notification_log rows

Shape detection is by request body keys — if `channels` is present it's
the new contract. Old callers keep working untouched.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.models.alert import (
    Alert,
    AlertCondition,
    CreateAlertRequest as NewCreateAlertRequest,
    DEFAULT_USER_ID,
    NotificationMessage,
    TestNotificationRequest,
    UpdateAlertRequest,
)
from app.services import notifications as notif
from app.services.alert_checker import (
    create_alert as legacy_create_alert,
    delete_alert as legacy_delete_alert,
    get_active_alerts as legacy_get_active_alerts,
    get_triggered_alerts as legacy_get_triggered_alerts,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


# ----- Legacy request body (kept verbatim) -----------------------------------


class LegacyCreateAlertRequest(BaseModel):
    symbol: str
    target_price: float
    condition: str  # "above" | "below"
    note: Optional[str] = None

    @field_validator("condition")
    @classmethod
    def _validate_condition(cls, v: str) -> str:
        if v not in ("above", "below"):
            raise ValueError("condition must be 'above' or 'below'")
        return v

    @field_validator("target_price")
    @classmethod
    def _validate_target_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("target_price must be positive")
        return v

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be empty")
        return v


# ----- POST /api/alerts (dual-shape) -----------------------------------------


@router.post("")
async def create_alert(request: Request) -> dict[str, Any]:
    """Create an alert. Accepts both legacy and new shapes.

    Shape is detected by the presence of `channels` in the body.
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="invalid JSON body") from e

    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")

    if "channels" in body:
        # New multi-channel shape.
        try:
            req = NewCreateAlertRequest(**body)
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        await notif.init_notifications_schema()
        alert = await notif.create_alert(
            symbol=req.symbol,
            condition=req.condition,
            channels=list(req.channels),
            note=req.note,
            active=req.active,
        )
        return {"alert": alert.model_dump()}

    # Legacy single-channel shape.
    try:
        legacy = LegacyCreateAlertRequest(**body)
    except Exception as e:
        # Pydantic v2 ValidationError → 422
        raise HTTPException(status_code=422, detail=str(e)) from None
    try:
        alert = await legacy_create_alert(
            symbol=legacy.symbol,
            target_price=legacy.target_price,
            condition=legacy.condition,
            current_price=None,
            note=legacy.note,
        )
        return {"alert": alert}
    except Exception as e:
        logger.exception("legacy create_alert failed")
        raise HTTPException(status_code=500, detail="Failed to create alert") from e


# ----- GET /api/alerts (legacy shape — preserves existing test contract) ----


@router.get("")
async def list_alerts() -> dict[str, Any]:
    """Return active legacy price alerts. New-shape consumers use /v2."""
    try:
        alerts = await legacy_get_active_alerts()
        return {"alerts": alerts}
    except Exception as e:
        logger.exception("list active alerts failed")
        raise HTTPException(status_code=500, detail="Failed to list alerts") from e


@router.get("/history")
async def list_history() -> dict[str, Any]:
    """Return triggered (inactive) legacy price alerts."""
    try:
        alerts = await legacy_get_triggered_alerts()
        return {"alerts": alerts}
    except Exception as e:
        logger.exception("list triggered alerts failed")
        raise HTTPException(status_code=500, detail="Failed to list alert history") from e


# ----- New-shape listing -----------------------------------------------------


@router.get("/v2")
async def list_alerts_v2(active_only: bool = False) -> dict[str, Any]:
    """List multi-channel alerts (new shape)."""
    await notif.init_notifications_schema()
    rows = await notif.list_alerts(active_only=active_only)
    return {"alerts": [a.model_dump() for a in rows]}


@router.get("/{alert_id}")
async def get_alert(alert_id: str) -> dict[str, Any]:
    await notif.init_notifications_schema()
    alert = await notif.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return {"alert": alert.model_dump()}


@router.patch("/{alert_id}")
async def patch_alert(alert_id: str, body: UpdateAlertRequest) -> dict[str, Any]:
    await notif.init_notifications_schema()
    updated = await notif.update_alert(
        alert_id,
        active=body.active,
        channels=list(body.channels) if body.channels is not None else None,
        note=body.note,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return {"alert": updated.model_dump()}


@router.delete("/{alert_id}")
async def delete_alert(alert_id: str) -> dict[str, Any]:
    """Delete an alert. Tries new-shape first, falls back to legacy.

    This keeps existing extension + test deletion paths working while
    also handling alerts created via the multi-channel API.
    """
    await notif.init_notifications_schema()
    if await notif.delete_alert(alert_id):
        return {"ok": True}
    try:
        legacy_deleted = await legacy_delete_alert(alert_id)
    except Exception as e:
        logger.exception("legacy delete_alert failed")
        raise HTTPException(status_code=500, detail="Failed to delete alert") from e
    if legacy_deleted:
        return {"ok": True}
    raise HTTPException(status_code=404, detail="alert not found")


# ----- Test send -------------------------------------------------------------


@router.post("/test")
async def send_test(body: TestNotificationRequest) -> dict[str, Any]:
    """Send a test notification through the requested channels.

    Returns per-channel `DeliveryResult` so a UI can show which channels
    are wired up correctly. We bypass dedup/throttle for this endpoint
    by using a unique fingerprint per call (random alert_id).
    """
    await notif.init_notifications_schema()
    cfg = await notif.load_provider_config()
    channels = notif.build_channels(body.channels, cfg)
    if not channels:
        raise HTTPException(status_code=400, detail="no usable channels")

    message = NotificationMessage(
        alert_id=None,  # bypasses dedup
        user_id=DEFAULT_USER_ID,
        symbol=body.symbol,
        title="agentX test",
        body=body.message,
        metadata={"test": True},
    )
    results = await notif.route_message(message, channels)
    return {"results": [r.model_dump() for r in results]}


# ----- Provider config (non-settings.py routes — keeps that file untouched) --


# Keys the alerts router is allowed to write into the `settings` table.
# Anything outside this list is silently dropped to prevent the alerts
# config endpoint from being a backdoor into LLM keys / thresholds.
_ALLOWED_PROVIDER_KEYS = frozenset({
    "telegram_bot_token", "telegram_chat_id",
    "smtp_host", "smtp_port", "smtp_username", "smtp_password",
    "smtp_from", "smtp_to", "smtp_use_tls",
    "whatsapp_provider",
    "twilio_account_sid", "twilio_auth_token",
    "twilio_whatsapp_from", "twilio_whatsapp_to",
    "twilio_sms_from", "twilio_sms_to",
    "gupshup_api_key", "gupshup_source", "gupshup_destination", "gupshup_src_name",
    "sms_provider",
    "msg91_auth_key", "msg91_flow_id", "msg91_mobile",
})


class ProviderConfigRequest(BaseModel):
    config: dict[str, str] = Field(default_factory=dict)


@router.get("/config")
async def get_provider_config() -> dict[str, Any]:
    """Return provider config with secrets redacted to `<key>_configured: bool`."""
    raw = await notif.load_provider_config()
    out: dict[str, Any] = {}
    for k in _ALLOWED_PROVIDER_KEYS:
        v = raw.get(k)
        if notif.is_secret_key(k):
            out[f"{k}_configured"] = bool(v)
        else:
            out[k] = v or ""
    return {"config": out}


@router.post("/config")
async def upsert_provider_config(body: ProviderConfigRequest) -> dict[str, Any]:
    """Upsert provider config keys. Unknown keys are dropped."""
    accepted = {k: v for k, v in (body.config or {}).items() if k in _ALLOWED_PROVIDER_KEYS}
    n = await notif.upsert_provider_config(accepted)
    return {"updated": n, "keys": sorted(accepted.keys())}


# ----- Diagnostics -----------------------------------------------------------


@router.get("/log/recent")
async def recent_log(limit: int = 100) -> dict[str, Any]:
    """Recent rows from notification_log. Cap limit defensively."""
    import aiosqlite

    from app.database import DB_PATH

    limit = max(1, min(int(limit), 500))
    await notif.init_notifications_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, ts, alert_id, user_id, channel, status, provider_id,
                      error, attempts
               FROM notification_log
               ORDER BY ts DESC
               LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return {"log": [dict(r) for r in rows]}


@router.get("/{alert_id}/events")
async def alert_events(alert_id: str, limit: int = 100) -> dict[str, Any]:
    import aiosqlite

    from app.database import DB_PATH

    limit = max(1, min(int(limit), 500))
    await notif.init_notifications_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, alert_id, ts, message, fired_at
               FROM alert_events
               WHERE alert_id = ?
               ORDER BY ts DESC
               LIMIT ?""",
            (alert_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return {"events": [dict(r) for r in rows]}
