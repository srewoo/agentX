"""User settings endpoints."""
import json
import logging

import aiosqlite
from fastapi import APIRouter

from app.database import DB_PATH
from app.models import UpdateSettingsRequest
from app.services.orchestrator import orchestrator
from app.services.secrets import SECRET_KEYS, get_manager

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

# Keys whose VALUES must never be returned to clients. We expose only a
# boolean `<key>_configured` flag so the UI can show "Set / Not set" without
# leaking the secret itself. The full SECRET_KEYS allowlist (used for
# at-rest encryption) is the source of truth — we restrict redaction to
# the subset that is exposed via this router.
_SECRET_KEYS = frozenset(SECRET_KEYS & ALLOWED_KEYS) if False else frozenset({
    "openai_api_key",
    "gemini_api_key",
    "claude_api_key",
    "llm_api_key",
})


def _redact_secrets(settings_map: dict) -> dict:
    """Return a copy of settings_map with secret values replaced by a
    `<key>_configured: bool` flag derived from non-emptiness.

    The original key is removed entirely so the secret never leaves the
    process. Safe to call on any dict shape.
    """
    redacted: dict = {}
    for key, value in settings_map.items():
        if key in _SECRET_KEYS:
            configured = bool(value) and str(value).strip() != ""
            redacted[f"{key}_configured"] = configured
            continue
        redacted[key] = value
    # Ensure every secret key has a corresponding flag, even if it was
    # missing from the DB row set.
    for key in _SECRET_KEYS:
        flag = f"{key}_configured"
        if flag not in redacted:
            redacted[flag] = False
    return redacted


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
    return {"settings": _redact_secrets(result)}


@router.post("")
async def update_settings(body: UpdateSettingsRequest):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"settings": {}}

    interval_changed = "alert_interval_minutes" in updates

    secrets_manager = get_manager()
    async with aiosqlite.connect(DB_PATH) as db:
        for key, value in updates.items():
            if key not in ALLOWED_KEYS:
                continue
            # Serialize lists to JSON
            if isinstance(value, list):
                value = json.dumps(value)
            else:
                value = str(value)
            # Seal any SECRET_KEYS value at the persistence boundary so the
            # SQLite file never contains plaintext credentials.
            if key in SECRET_KEYS and value != "":
                value = secrets_manager.seal_key(value)
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

    # Never echo secret values back. Redact so tests and clients see the
    # `_configured` flag instead of the raw key.
    return {"settings": _redact_secrets(updates), "ok": True}
