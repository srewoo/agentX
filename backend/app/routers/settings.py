"""User settings endpoints."""
import json
import logging

import aiosqlite
from fastapi import APIRouter

from app.database import DB_PATH
from app.models import UpdateSettingsRequest, UpstoxExchangeRequest
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
    "llm_judging_enabled", "debate_enabled", "multi_perspective_enabled",
    # Advisor + autonomous-paper-trading toggles
    "auto_paper_trade", "auto_paper_min_strength", "auto_paper_max_open",
    "auto_paper_trade_enabled", "auto_paper_min_conviction", "auto_paper_max_per_day",
    "auto_paper_max_open_positions", "auto_paper_interval_minutes",
    # Daily lightweight backtest pulse (runs 11:00 IST).
    "daily_backtest_enabled", "daily_backtest_symbols",
    "capital", "risk_per_trade_pct", "atr_sl_mult", "atr_target_mult",
    "regime_filter", "roundtrip_cost_pct", "dedupe_signals",
    "audio_alerts", "audio_strength_threshold",
    # Broker integration (AngelOne SmartAPI + Kite Connect).
    "broker",
    "angelone_api_key", "angelone_client_code", "angelone_mpin", "angelone_totp_secret",
    "kite_api_key", "kite_api_secret", "kite_access_token",
    # Upstox data source (daily OAuth token + app creds) + Twelve Data fallback.
    "upstox_access_token", "upstox_api_key", "upstox_api_secret",
    "twelvedata_api_key",
    # Financial Modeling Prep (fundamentals + earnings calendar) + Finnhub (macro).
    "fmp_api_key", "finnhub_api_key",
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
    # Broker credentials must never round-trip to the client either; the
    # UI sees only the `<key>_configured` boolean flag.
    "angelone_api_key", "angelone_client_code", "angelone_mpin", "angelone_totp_secret",
    "kite_api_key", "kite_api_secret", "kite_access_token",
    "upstox_access_token", "upstox_api_key", "upstox_api_secret",
    "twelvedata_api_key",
    "fmp_api_key", "finnhub_api_key",
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
            # Serialize lists to JSON; normalize booleans so the JS client can
            # read them back with a strict `=== "true"` check (Python's
            # `str(True)` returns "True" — different bytes from JS "true").
            if isinstance(value, list):
                value = json.dumps(value)
            elif isinstance(value, bool):
                value = "true" if value else "false"
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


@router.post("/test-upstox")
async def test_upstox() -> dict:
    """Validate the stored Upstox access token against /v2/user/profile.

    Reads the (sealed) ``upstox_access_token`` from settings, unseals it, and
    hits Upstox to confirm it's live. Returns ``{ok, message}`` for the
    Settings UI button. Never returns the token itself.
    """
    from app.services.orchestrator import _get_settings
    from app.services import upstox_fetcher

    settings = await _get_settings()
    token = settings.get("upstox_access_token") or ""
    if not token:
        return {"ok": False, "message": "No Upstox access token saved. Paste one and Save first."}
    return await upstox_fetcher.test_connection(token)


@router.get("/upstox-login-url")
async def upstox_login_url(redirect_uri: str) -> dict:
    """Build the Upstox OAuth login URL from the stored ``upstox_api_key``.

    The caller opens the returned URL in a browser, approves access, and Upstox
    redirects to ``redirect_uri?code=<CODE>``. ``redirect_uri`` must match the
    one registered on the Upstox app. The api_key is *not* a secret in the URL —
    it is the public client_id — so returning it here is safe.
    """
    from app.services.orchestrator import _get_settings
    from app.services import upstox_fetcher

    settings = await _get_settings()
    api_key = settings.get("upstox_api_key") or ""
    if not api_key:
        return {"ok": False, "message": "Save your Upstox API key (upstox_api_key) first."}
    return {"ok": True, "url": upstox_fetcher.build_login_url(api_key, redirect_uri)}


@router.post("/upstox-exchange-code")
async def upstox_exchange_code(body: UpstoxExchangeRequest) -> dict:
    """Trade an OAuth authorization code for an access token and store it.

    Reads the stored (sealed) ``upstox_api_key`` / ``upstox_api_secret``,
    exchanges ``code`` for an access token, seals it, and persists it as
    ``upstox_access_token``. Returns ``{ok, message}`` only — never the token.
    """
    from app.services.orchestrator import _get_settings
    from app.services import upstox_fetcher

    settings = await _get_settings()
    api_key = settings.get("upstox_api_key") or ""
    api_secret = settings.get("upstox_api_secret") or ""
    if not (api_key and api_secret):
        return {"ok": False, "message": "Save upstox_api_key and upstox_api_secret first."}

    result = await upstox_fetcher.exchange_code(
        body.code, api_key=api_key, api_secret=api_secret, redirect_uri=body.redirect_uri,
    )
    if not result.get("ok"):
        return {"ok": False, "message": result.get("message", "Token exchange failed.")}

    secrets_manager = get_manager()
    sealed = secrets_manager.seal_key(result["access_token"])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("upstox_access_token", sealed),
        )
        await db.commit()
    return {"ok": True, "message": "Upstox access token generated and saved."}
