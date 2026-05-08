"""Notification routing service.

Responsibilities:
  1. CRUD for `alerts` (the new multi-channel table).
  2. Append-only `alert_events` and `notification_log` tables.
  3. Fan-out a NotificationMessage across N channels in parallel with
     retry, dedup, and throttle.
  4. Evaluate alert conditions against a market snapshot.

This file deliberately keeps DB access local (no repository abstraction
yet — single-file SQLite, single bounded context). When we move to a
real DB we can extract a repo without touching channels or routers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional
from uuid import uuid4

import aiosqlite

from app.database import DB_PATH
from app.models.alert import (
    DEFAULT_USER_ID,
    Alert,
    AlertCondition,
    DeliveryResult,
    NotificationMessage,
)
from app.services.channels import NotificationChannel

logger = logging.getLogger(__name__)

# ---- Tunables (named constants — never hardcoded magic numbers in callers) --

# Retry policy. We retry transient failures (5xx / network) up to
# MAX_ATTEMPTS - 1 extra times; 4xx errors short-circuit.
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5
BACKOFF_JITTER_SECONDS = 0.25

# Dedup window — if the same (alert_id, message-fingerprint) was sent
# inside this window, we drop the duplicate. 60s matches the spec.
DEDUP_WINDOW_SECONDS = 60

# Throttle: per (user_id, channel) sliding window. Caller can override
# per-channel via `THROTTLE_LIMITS_PER_HOUR`.
DEFAULT_THROTTLE_PER_HOUR = 60
THROTTLE_LIMITS_PER_HOUR: dict[str, int] = {
    "telegram": 60,
    "email": 30,
    "whatsapp": 30,
    "sms": 20,
    "push": 120,
}

# Provider-config keys we accept in the `settings` table. Listed here so
# tests and callers can introspect without grepping. We never log values
# for these keys.
_SECRET_SETTING_KEYS = frozenset({
    "telegram_bot_token",
    "smtp_password",
    "twilio_auth_token",
    "gupshup_api_key",
    "msg91_auth_key",
})


# --------- Schema bootstrap (idempotent) --------------------------------------

CREATE_ALERTS_TABLE = """
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    condition_kind TEXT NOT NULL,
    condition_payload_json TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_at TEXT NOT NULL
);
"""

CREATE_ALERT_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS alert_events (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    message TEXT NOT NULL,
    fired_at TEXT NOT NULL
);
"""

CREATE_NOTIFICATION_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS notification_log (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    alert_id TEXT,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_id TEXT,
    error TEXT,
    fingerprint TEXT,
    attempts INTEGER DEFAULT 1
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_alerts_user_active ON alerts(user_id, active);",
    "CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events(alert_id, ts DESC);",
    "CREATE INDEX IF NOT EXISTS idx_notif_log_ts ON notification_log(ts DESC);",
    "CREATE INDEX IF NOT EXISTS idx_notif_log_user_channel_ts ON notification_log(user_id, channel, ts DESC);",
    "CREATE INDEX IF NOT EXISTS idx_notif_log_dedup ON notification_log(alert_id, fingerprint, ts DESC);",
]


async def init_notifications_schema() -> None:
    """Create notification tables if missing. Safe to call repeatedly."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.execute(CREATE_ALERTS_TABLE)
        await db.execute(CREATE_ALERT_EVENTS_TABLE)
        await db.execute(CREATE_NOTIFICATION_LOG_TABLE)
        for sql in CREATE_INDEXES:
            await db.execute(sql)
        await db.commit()


# --------- Alert CRUD ---------------------------------------------------------


async def create_alert(
    *,
    symbol: str,
    condition: AlertCondition,
    channels: list[str],
    note: Optional[str],
    user_id: str = DEFAULT_USER_ID,
    active: bool = True,
) -> Alert:
    """Persist a new alert and return it."""
    alert = Alert(
        id=str(uuid4()),
        user_id=user_id,
        symbol=symbol.upper(),
        condition=condition,
        channels=list(channels),  # type: ignore[arg-type]
        active=active,
        note=note,
        created_at=Alert.now_iso(),
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO alerts
               (id, user_id, symbol, condition_kind, condition_payload_json,
                channels_json, active, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                alert.id, alert.user_id, alert.symbol,
                alert.condition.kind, json.dumps(alert.condition.payload),
                json.dumps(alert.channels),
                1 if alert.active else 0,
                alert.note, alert.created_at,
            ),
        )
        await db.commit()
    logger.info(
        "alert created",
        extra={"alert_id": alert.id, "symbol": alert.symbol, "kind": alert.condition.kind},
    )
    return alert


async def list_alerts(user_id: str = DEFAULT_USER_ID, active_only: bool = False) -> list[Alert]:
    sql = "SELECT * FROM alerts WHERE user_id = ?"
    args: list[Any] = [user_id]
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY created_at DESC LIMIT 500"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()
    return [_row_to_alert(r) for r in rows]


async def get_alert(alert_id: str, user_id: str = DEFAULT_USER_ID) -> Optional[Alert]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM alerts WHERE id = ? AND user_id = ? LIMIT 1",
            (alert_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_alert(row) if row else None


async def update_alert(
    alert_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    active: Optional[bool] = None,
    channels: Optional[list[str]] = None,
    note: Optional[str] = None,
) -> Optional[Alert]:
    sets: list[str] = []
    args: list[Any] = []
    if active is not None:
        sets.append("active = ?")
        args.append(1 if active else 0)
    if channels is not None:
        sets.append("channels_json = ?")
        args.append(json.dumps(channels))
    if note is not None:
        sets.append("note = ?")
        args.append(note)
    if not sets:
        return await get_alert(alert_id, user_id)
    args.extend([alert_id, user_id])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE alerts SET {', '.join(sets)} WHERE id = ? AND user_id = ?",  # noqa: S608 — sets list is closed
            args,
        )
        await db.commit()
        if cur.rowcount == 0:
            return None
    return await get_alert(alert_id, user_id)


async def delete_alert(alert_id: str, user_id: str = DEFAULT_USER_ID) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


def _row_to_alert(row: aiosqlite.Row) -> Alert:
    return Alert(
        id=row["id"],
        user_id=row["user_id"],
        symbol=row["symbol"],
        condition=AlertCondition(
            kind=row["condition_kind"],
            payload=json.loads(row["condition_payload_json"] or "{}"),
        ),
        channels=json.loads(row["channels_json"] or "[]"),
        active=bool(row["active"]),
        note=row["note"],
        created_at=row["created_at"],
    )


# --------- Provider config from settings table --------------------------------


async def load_provider_config() -> dict[str, str]:
    """Read all settings rows. Caller filters to channel-relevant keys.

    Values stored as `enc:v1:...` (any key in `SECRET_KEYS`) are decrypted
    transparently — channels (Telegram bot token, SMTP password, Twilio
    auth token, etc.) need plaintext to authenticate with their upstream
    APIs. We still never log the returned dict.
    """
    from app.database import _decrypt_settings_map

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
    raw = {r["key"]: r["value"] for r in rows}
    return _decrypt_settings_map(raw)


async def upsert_provider_config(updates: dict[str, str]) -> int:
    """Idempotent UPSERT into the settings KV table.

    Returns the number of keys written. Values for `_SECRET_SETTING_KEYS`
    are stored verbatim — production should encrypt at rest; we use the
    same column for parity with existing LLM API keys.
    """
    if not updates:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        for k, v in updates.items():
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )
        await db.commit()
    redacted_keys = [k for k in updates if k in _SECRET_SETTING_KEYS]
    if redacted_keys:
        logger.info("provider config updated", extra={"redacted_keys": redacted_keys})
    return len(updates)


def is_secret_key(key: str) -> bool:
    return key in _SECRET_SETTING_KEYS


# --------- Channel factory ----------------------------------------------------


def build_channels(
    names: Iterable[str], provider_config: dict[str, Any]
) -> dict[str, NotificationChannel]:
    """Instantiate the requested channel adapters from provider config.

    Local imports keep optional deps (aiosmtplib, etc.) from being
    required when the corresponding channel is disabled.
    """
    out: dict[str, NotificationChannel] = {}
    wanted = set(names)
    if "telegram" in wanted:
        from app.services.channels.telegram import TelegramChannel
        out["telegram"] = TelegramChannel(
            bot_token=str(provider_config.get("telegram_bot_token") or ""),
            chat_id=str(provider_config.get("telegram_chat_id") or ""),
        )
    if "email" in wanted:
        from app.services.channels.email import EmailChannel
        out["email"] = EmailChannel(
            host=str(provider_config.get("smtp_host") or ""),
            port=int(provider_config.get("smtp_port") or 0) or 587,
            username=str(provider_config.get("smtp_username") or ""),
            password=str(provider_config.get("smtp_password") or ""),
            from_addr=str(provider_config.get("smtp_from") or ""),
            to_addr=str(provider_config.get("smtp_to") or ""),
            use_tls=str(provider_config.get("smtp_use_tls") or "true").lower() != "false",
        )
    if "whatsapp" in wanted:
        from app.services.channels.whatsapp import WhatsAppChannel
        out["whatsapp"] = WhatsAppChannel(**provider_config)
    if "sms" in wanted:
        from app.services.channels.sms import SmsChannel
        out["sms"] = SmsChannel(**provider_config)
    if "push" in wanted:
        from app.services.channels.push import PushChannel
        out["push"] = PushChannel()
    return out


# --------- Dedup + throttle ---------------------------------------------------


def fingerprint(message: NotificationMessage) -> str:
    """Stable hash for dedup. Includes title + body + symbol + alert_id."""
    h = hashlib.sha256()
    h.update((message.alert_id or "").encode())
    h.update(b"|")
    h.update((message.symbol or "").encode())
    h.update(b"|")
    h.update(message.title.encode())
    h.update(b"|")
    h.update(message.body.encode())
    return h.hexdigest()[:32]


async def _was_recently_sent(
    alert_id: Optional[str], fp: str, window_seconds: int
) -> bool:
    if not alert_id:
        return False
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    ).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM notification_log
               WHERE alert_id = ? AND fingerprint = ? AND status = 'delivered'
                 AND ts >= ?
               LIMIT 1""",
            (alert_id, fp, cutoff),
        ) as cur:
            row = await cur.fetchone()
    return row is not None


async def _is_throttled(user_id: str, channel: str, limit_per_hour: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*) AS c FROM notification_log
               WHERE user_id = ? AND channel = ? AND ts >= ?
                 AND status IN ('delivered','failed')""",
            (user_id, channel, cutoff),
        ) as cur:
            row = await cur.fetchone()
    count = int(row[0]) if row else 0
    return count >= limit_per_hour


async def _log_delivery(
    *,
    alert_id: Optional[str],
    user_id: str,
    result: DeliveryResult,
    fp: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO notification_log
               (id, ts, alert_id, user_id, channel, status, provider_id, error,
                fingerprint, attempts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid4()),
                datetime.now(timezone.utc).isoformat(),
                alert_id,
                user_id,
                result.channel,
                result.status,
                result.provider_id,
                _redact_error(result.error),
                fp,
                result.attempts,
            ),
        )
        await db.commit()


def _redact_error(err: Optional[str]) -> Optional[str]:
    """Strip anything that looks like a token from error messages.

    Defence in depth — adapters shouldn't put secrets in errors, but we
    don't want a single sloppy adapter to leak via the log table.
    """
    if not err:
        return err
    out = err
    for needle in ("bot", "token", "password", "auth", "key", "secret"):
        # crude: collapse `<needle>=...` to `<needle>=***`
        marker = f"{needle}="
        idx = out.lower().find(marker)
        if idx != -1:
            end = out.find(" ", idx)
            end = end if end != -1 else len(out)
            out = out[: idx + len(marker)] + "***" + out[end:]
    return out


# --------- Send with retry ----------------------------------------------------


async def _send_with_retry(
    channel: NotificationChannel,
    message: NotificationMessage,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], "asyncio.Future[None]"] = asyncio.sleep,
) -> DeliveryResult:
    """Send via a channel, retrying transient failures with exp backoff + jitter.

    Transient = error tagged "server:" by the adapter. "client:" failures
    are terminal — don't waste retries on a bad token.
    """
    last: Optional[DeliveryResult] = None
    for attempt in range(1, max_attempts + 1):
        result = await channel.send(message)
        result.attempts = attempt
        if result.ok or result.status == "skipped":
            return result
        # Don't retry client errors.
        if (result.error or "").startswith("client:"):
            return result
        last = result
        if attempt < max_attempts:
            backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            backoff += random.random() * BACKOFF_JITTER_SECONDS
            await sleep(backoff)
    # Exhausted retries.
    assert last is not None
    return last


# --------- Public: route message to channels ---------------------------------


async def route_message(
    message: NotificationMessage,
    channels: dict[str, NotificationChannel],
    *,
    user_id: str = DEFAULT_USER_ID,
    dedup_window_seconds: int = DEDUP_WINDOW_SECONDS,
    throttle_limits: Optional[dict[str, int]] = None,
) -> list[DeliveryResult]:
    """Fan out `message` to all `channels` in parallel.

    Per-channel pipeline:
        dedup-check → throttle-check → send-with-retry → log
    """
    limits = {**THROTTLE_LIMITS_PER_HOUR, **(throttle_limits or {})}
    fp = fingerprint(message)

    async def _one(channel_name: str, channel: NotificationChannel) -> DeliveryResult:
        # Dedup is global per (alert, fingerprint) regardless of channel —
        # if we already sent this exact message via Telegram 10s ago, the
        # email shouldn't be deduped. So: dedup is per-channel here.
        if await _was_recently_sent_for_channel(
            message.alert_id, channel_name, fp, dedup_window_seconds
        ):
            res = DeliveryResult(channel=channel_name, ok=False, status="deduped",
                                 error="client:deduped within window")
            await _log_delivery(alert_id=message.alert_id, user_id=user_id, result=res, fp=fp)
            return res

        limit = limits.get(channel_name, DEFAULT_THROTTLE_PER_HOUR)
        if await _is_throttled(user_id, channel_name, limit):
            res = DeliveryResult(channel=channel_name, ok=False, status="throttled",
                                 error="client:throttle limit hit")
            await _log_delivery(alert_id=message.alert_id, user_id=user_id, result=res, fp=fp)
            return res

        start = time.perf_counter()
        result = await _send_with_retry(channel, message)
        if result.duration_ms is None:
            result.duration_ms = int((time.perf_counter() - start) * 1000)
        await _log_delivery(alert_id=message.alert_id, user_id=user_id, result=result, fp=fp)
        return result

    coros = [_one(name, ch) for name, ch in channels.items()]
    # return_exceptions=True so one channel's surprise crash doesn't drop
    # the others' results — but channels shouldn't raise; they should
    # return DeliveryResult(ok=False).
    raw = await asyncio.gather(*coros, return_exceptions=True)
    out: list[DeliveryResult] = []
    for r in raw:
        if isinstance(r, BaseException):
            logger.exception("channel raised — adapter contract violated")
            out.append(DeliveryResult(
                channel="unknown", ok=False, status="failed",
                error=f"server:adapter raised {type(r).__name__}",
            ))
        else:
            out.append(r)
    return out


async def _was_recently_sent_for_channel(
    alert_id: Optional[str], channel: str, fp: str, window_seconds: int
) -> bool:
    if not alert_id:
        return False
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    ).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM notification_log
               WHERE alert_id = ? AND channel = ? AND fingerprint = ?
                 AND status = 'delivered' AND ts >= ?
               LIMIT 1""",
            (alert_id, channel, fp, cutoff),
        ) as cur:
            row = await cur.fetchone()
    return row is not None


# --------- Condition evaluation ----------------------------------------------


async def evaluate_condition(
    condition: AlertCondition,
    *,
    symbol: str,
    snapshot: dict[str, Any],
) -> bool:
    """Return True if `condition` is met given `snapshot`.

    `snapshot` keys (all optional, kind-dependent):
      - last_price: float
      - prev_close: float
      - pct_change_1d: float            # signed %
      - volume: float
      - volume_avg: float
      - n_day_high: float               # for breakout
      - close: float                    # for breakout
      - recommendation_conviction: int  # 0..10
    """
    kind = condition.kind
    p = condition.payload or {}

    if kind == "price_above":
        last = _f(snapshot.get("last_price"))
        target = _f(p.get("price"))
        return last is not None and target is not None and last > target

    if kind == "price_below":
        last = _f(snapshot.get("last_price"))
        target = _f(p.get("price"))
        return last is not None and target is not None and last < target

    if kind == "pct_change_1d_above":
        chg = _f(snapshot.get("pct_change_1d"))
        thr = _f(p.get("pct"))
        return chg is not None and thr is not None and chg > thr

    if kind == "pct_change_1d_below":
        chg = _f(snapshot.get("pct_change_1d"))
        thr = _f(p.get("pct"))
        return chg is not None and thr is not None and chg < thr

    if kind == "recommendation_conviction_above":
        score = _f(snapshot.get("recommendation_conviction"))
        thr = _f(p.get("score"))
        return score is not None and thr is not None and score >= thr

    if kind == "volume_spike_above":
        vol = _f(snapshot.get("volume"))
        avg = _f(snapshot.get("volume_avg"))
        ratio = _f(p.get("ratio"))
        if vol is None or avg is None or ratio is None or avg <= 0:
            return False
        return (vol / avg) >= ratio

    if kind == "breakout":
        close = _f(snapshot.get("close")) or _f(snapshot.get("last_price"))
        n_high = _f(snapshot.get("n_day_high"))
        return close is not None and n_high is not None and close > n_high

    logger.warning("unknown condition kind", extra={"kind": kind, "symbol": symbol})
    return False


def _f(v: Any) -> Optional[float]:
    """Coerce to float; tolerate None and bad types without raising."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------- Alert event recording ---------------------------------------------


async def record_alert_event(alert_id: str, message: str) -> None:
    """Persist that an alert fired. Used by the scheduler that calls evaluate."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO alert_events (id, alert_id, ts, message, fired_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(uuid4()), alert_id, now, message, now),
        )
        await db.commit()
