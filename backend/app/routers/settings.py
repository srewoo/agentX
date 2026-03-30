"""User settings endpoints."""
import json
import logging

import aiosqlite
from fastapi import APIRouter

from app.database import DB_PATH
from app.models import UpdateSettingsRequest
from app.services.orchestrator import orchestrator

router = APIRouter(prefix="/api/settings", tags=["settings"])
logger = logging.getLogger(__name__)

ALLOWED_KEYS = {
    "alert_interval_minutes", "risk_mode", "signal_types",
    "llm_provider", "llm_model", "llm_api_key",
    "openai_api_key", "gemini_api_key", "claude_api_key",
    # Configurable signal thresholds
    "rsi_overbought", "rsi_oversold", "price_spike_pct",
    "volume_spike_ratio", "breakout_min_score",
}


@router.get("")
async def get_settings():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM settings") as cursor:
            rows = await cursor.fetchall()
    result = {row["key"]: row["value"] for row in rows}
    # Parse signal_types JSON array
    if "signal_types" in result:
        try:
            result["signal_types"] = json.loads(result["signal_types"])
        except Exception:
            pass
    return {"settings": result}


@router.post("")
async def update_settings(body: UpdateSettingsRequest):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"settings": {}}

    interval_changed = "alert_interval_minutes" in updates

    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in updates.items():
            if key not in ALLOWED_KEYS:
                continue
            # Serialize lists to JSON
            if isinstance(value, list):
                value = json.dumps(value)
            else:
                value = str(value)
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()

    # Restart orchestrator loop if interval changed
    if interval_changed and orchestrator.is_running():
        await orchestrator.stop()
        await orchestrator.start()
        logger.info(f"Orchestrator restarted with new interval: {updates['alert_interval_minutes']} min")

    return {"settings": updates, "ok": True}
