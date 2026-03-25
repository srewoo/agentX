from __future__ import annotations

"""Price alerts API router."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.services.alert_checker import (
    create_alert,
    delete_alert,
    get_active_alerts,
    get_triggered_alerts,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class CreateAlertRequest(BaseModel):
    symbol: str
    target_price: float
    condition: str  # "above" or "below"
    note: Optional[str] = None

    @field_validator("condition")
    @classmethod
    def validate_condition(cls, v: str) -> str:
        if v not in ("above", "below"):
            raise ValueError("condition must be 'above' or 'below'")
        return v

    @field_validator("target_price")
    @classmethod
    def validate_target_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("target_price must be positive")
        return v

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be empty")
        return v


@router.get("")
async def list_active_alerts():
    """List all active (untriggered) price alerts."""
    try:
        alerts = await get_active_alerts()
        return {"alerts": alerts}
    except Exception as e:
        logger.error(f"Failed to list active alerts: {e}")
        raise HTTPException(status_code=500, detail="Failed to list alerts")


@router.post("")
async def create_price_alert(body: CreateAlertRequest):
    """Create a new price alert."""
    try:
        alert = await create_alert(
            symbol=body.symbol,
            target_price=body.target_price,
            condition=body.condition,
            current_price=None,
            note=body.note,
        )
        return {"alert": alert}
    except Exception as e:
        logger.error(f"Failed to create alert: {e}")
        raise HTTPException(status_code=500, detail="Failed to create alert")


@router.delete("/{alert_id}")
async def remove_alert(alert_id: str):
    """Delete a price alert by ID."""
    try:
        deleted = await delete_alert(alert_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Alert not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete alert {alert_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete alert")


@router.get("/history")
async def list_triggered_alerts():
    """List all triggered (inactive) price alerts."""
    try:
        alerts = await get_triggered_alerts()
        return {"alerts": alerts}
    except Exception as e:
        logger.error(f"Failed to list triggered alerts: {e}")
        raise HTTPException(status_code=500, detail="Failed to list alert history")
