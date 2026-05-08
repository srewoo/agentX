"""Tests for the multi-channel notification service.

Covers:
  * fan-out across channels (asyncio.gather)
  * retry on transient (server:) failures
  * no-retry on terminal (client:) failures
  * dedup window
  * throttle limits
  * condition evaluation for each supported kind
  * alert CRUD round-trip
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import aiosqlite
import pytest

from app.models.alert import (
    AlertCondition,
    DeliveryResult,
    NotificationMessage,
)
from app.services import notifications as notif


# ---------- helpers ----------------------------------------------------------


class FakeChannel:
    """Programmable channel for testing the routing pipeline."""

    def __init__(
        self,
        name: str,
        results: list[DeliveryResult] | DeliveryResult | None = None,
        raise_on: int | None = None,
    ) -> None:
        self.name = name
        self.calls = 0
        if isinstance(results, list):
            self._results = list(results)
            self._single: DeliveryResult | None = None
        else:
            self._results = []
            self._single = results
        self._raise_on = raise_on

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.calls += 1
        if self._raise_on is not None and self.calls == self._raise_on:
            raise RuntimeError("synthetic adapter crash")
        if self._results:
            return self._results.pop(0)
        if self._single is not None:
            # clone so attempts mutation doesn't bleed across calls
            return self._single.model_copy()
        return DeliveryResult(channel=self.name, ok=True, status="delivered")

    async def healthcheck(self) -> bool:
        return True


@pytest.fixture()
def patched_db(tmp_db_path: str):
    """Point notifications module at the conftest test DB."""
    with patch("app.services.notifications.DB_PATH", tmp_db_path):
        yield tmp_db_path


@pytest.fixture()
async def schema(patched_db, db):
    """Ensure notification tables exist on the test DB."""
    await notif.init_notifications_schema()
    yield
    # Clean rows so each test is isolated.
    async with aiosqlite.connect(patched_db) as conn:
        for table in ("alerts", "alert_events", "notification_log"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except aiosqlite.OperationalError:
                pass
        await conn.commit()


# ---------- routing fan-out --------------------------------------------------


@pytest.mark.asyncio
async def test_route_message_fan_out_runs_channels_in_parallel(schema):
    msg = _msg(alert_id=str(uuid4()))
    a = FakeChannel("telegram")
    b = FakeChannel("email")
    c = FakeChannel("sms")
    results = await notif.route_message(msg, {"telegram": a, "email": b, "sms": c})
    assert {r.channel for r in results} == {"telegram", "email", "sms"}
    assert all(r.ok for r in results)
    assert a.calls == b.calls == c.calls == 1


@pytest.mark.asyncio
async def test_route_message_swallows_adapter_exceptions(schema):
    msg = _msg(alert_id=str(uuid4()))
    bad = FakeChannel("telegram", raise_on=1)
    good = FakeChannel("email")
    results = await notif.route_message(msg, {"telegram": bad, "email": good})
    statuses = {r.channel: r.status for r in results}
    # Adapter that raised becomes 'failed' but does not break siblings.
    assert any(r.status == "failed" for r in results)
    assert statuses.get("email") == "delivered"


# ---------- retry ------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_with_retry_retries_server_errors_then_succeeds():
    sequence = [
        DeliveryResult(channel="x", ok=False, status="failed", error="server:boom"),
        DeliveryResult(channel="x", ok=False, status="failed", error="server:still boom"),
        DeliveryResult(channel="x", ok=True, status="delivered"),
    ]
    ch = FakeChannel("x", results=sequence)

    async def _no_sleep(_: float) -> None:
        return None

    msg = _msg()
    result = await notif._send_with_retry(ch, msg, sleep=_no_sleep)
    assert result.ok is True
    assert result.attempts == 3
    assert ch.calls == 3


@pytest.mark.asyncio
async def test_send_with_retry_does_not_retry_client_errors():
    sequence = [
        DeliveryResult(channel="x", ok=False, status="failed", error="client:bad token"),
        DeliveryResult(channel="x", ok=True, status="delivered"),  # never reached
    ]
    ch = FakeChannel("x", results=sequence)

    async def _no_sleep(_: float) -> None:
        return None

    result = await notif._send_with_retry(ch, _msg(), sleep=_no_sleep)
    assert result.ok is False
    assert result.attempts == 1
    assert ch.calls == 1


@pytest.mark.asyncio
async def test_send_with_retry_gives_up_after_max_attempts():
    sequence = [
        DeliveryResult(channel="x", ok=False, status="failed", error="server:1"),
        DeliveryResult(channel="x", ok=False, status="failed", error="server:2"),
        DeliveryResult(channel="x", ok=False, status="failed", error="server:3"),
    ]
    ch = FakeChannel("x", results=sequence)

    async def _no_sleep(_: float) -> None:
        return None

    result = await notif._send_with_retry(ch, _msg(), sleep=_no_sleep)
    assert result.ok is False
    assert result.attempts == notif.MAX_ATTEMPTS


# ---------- dedup ------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_drops_duplicate_within_window(schema):
    alert_id = str(uuid4())
    msg = _msg(alert_id=alert_id, body="same body")
    ch = FakeChannel("telegram")
    first = await notif.route_message(msg, {"telegram": ch})
    second = await notif.route_message(msg, {"telegram": ch})
    assert first[0].status == "delivered"
    assert second[0].status == "deduped"
    # The adapter only got hit once because the second was deduped before send.
    assert ch.calls == 1


@pytest.mark.asyncio
async def test_dedup_does_not_apply_across_alert_ids(schema):
    ch = FakeChannel("telegram")
    a = await notif.route_message(_msg(alert_id=str(uuid4()), body="hi"),
                                  {"telegram": ch})
    b = await notif.route_message(_msg(alert_id=str(uuid4()), body="hi"),
                                  {"telegram": ch})
    assert a[0].status == "delivered"
    assert b[0].status == "delivered"
    assert ch.calls == 2


# ---------- throttle ---------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_blocks_after_limit(schema, patched_db):
    user = "throttle-user"
    channel = "telegram"
    # Pre-fill log with 3 'delivered' rows in the last hour.
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(patched_db) as db_conn:
        for _ in range(3):
            await db_conn.execute(
                """INSERT INTO notification_log
                   (id, ts, alert_id, user_id, channel, status, fingerprint, attempts)
                   VALUES (?, ?, ?, ?, ?, 'delivered', ?, 1)""",
                (str(uuid4()), now, str(uuid4()), user, channel, "fp"),
            )
        await db_conn.commit()

    ch = FakeChannel(channel)
    msg = _msg(user_id=user, alert_id=str(uuid4()))
    results = await notif.route_message(
        msg, {channel: ch}, user_id=user, throttle_limits={channel: 3},
    )
    assert results[0].status == "throttled"
    assert ch.calls == 0


@pytest.mark.asyncio
async def test_throttle_window_expires(schema, patched_db):
    user = "throttle-user-2"
    channel = "telegram"
    # Old row outside the 1-hour window — should not count.
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    async with aiosqlite.connect(patched_db) as db_conn:
        await db_conn.execute(
            """INSERT INTO notification_log
               (id, ts, alert_id, user_id, channel, status, fingerprint, attempts)
               VALUES (?, ?, ?, ?, ?, 'delivered', ?, 1)""",
            (str(uuid4()), old, str(uuid4()), user, channel, "fp"),
        )
        await db_conn.commit()

    ch = FakeChannel(channel)
    results = await notif.route_message(
        _msg(user_id=user, alert_id=str(uuid4())),
        {channel: ch},
        user_id=user,
        throttle_limits={channel: 1},
    )
    assert results[0].status == "delivered"


# ---------- alert CRUD -------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_crud_roundtrip(schema):
    cond = AlertCondition(kind="price_above", payload={"price": 100.0})
    created = await notif.create_alert(
        symbol="reliance", condition=cond, channels=["telegram"], note="watch this",
    )
    assert created.symbol == "RELIANCE"
    assert created.active is True

    fetched = await notif.get_alert(created.id)
    assert fetched is not None and fetched.id == created.id

    updated = await notif.update_alert(
        created.id, active=False, channels=["telegram", "email"], note="muted",
    )
    assert updated is not None
    assert updated.active is False
    assert updated.channels == ["telegram", "email"]
    assert updated.note == "muted"

    deleted = await notif.delete_alert(created.id)
    assert deleted is True
    assert await notif.get_alert(created.id) is None


@pytest.mark.asyncio
async def test_update_alert_returns_none_for_unknown_id(schema):
    assert await notif.update_alert("does-not-exist", active=False) is None


# ---------- condition evaluation ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,payload,snapshot,expected",
    [
        ("price_above", {"price": 100}, {"last_price": 105}, True),
        ("price_above", {"price": 100}, {"last_price": 99}, False),
        ("price_below", {"price": 100}, {"last_price": 90}, True),
        ("pct_change_1d_above", {"pct": 3}, {"pct_change_1d": 5.0}, True),
        ("pct_change_1d_above", {"pct": 3}, {"pct_change_1d": 1.0}, False),
        ("pct_change_1d_below", {"pct": -3}, {"pct_change_1d": -4.0}, True),
        ("recommendation_conviction_above", {"score": 7},
         {"recommendation_conviction": 8}, True),
        ("recommendation_conviction_above", {"score": 7},
         {"recommendation_conviction": 6}, False),
        ("volume_spike_above", {"ratio": 2.0},
         {"volume": 300, "volume_avg": 100}, True),
        ("volume_spike_above", {"ratio": 2.0},
         {"volume": 150, "volume_avg": 100}, False),
        ("volume_spike_above", {"ratio": 2.0},
         {"volume": 150, "volume_avg": 0}, False),
        ("breakout", {"lookback_days": 20},
         {"close": 200, "n_day_high": 199}, True),
        ("breakout", {"lookback_days": 20},
         {"close": 198, "n_day_high": 200}, False),
    ],
)
async def test_evaluate_condition_truth_table(kind, payload, snapshot, expected):
    cond = AlertCondition(kind=kind, payload=payload)
    got = await notif.evaluate_condition(cond, symbol="X", snapshot=snapshot)
    assert got is expected


@pytest.mark.asyncio
async def test_evaluate_condition_unknown_kind_returns_false():
    # Bypass pydantic Literal by constructing via model_construct.
    cond = AlertCondition.model_construct(kind="nonexistent", payload={})
    got = await notif.evaluate_condition(cond, symbol="X", snapshot={"last_price": 1})
    assert got is False


# ---------- log redaction ----------------------------------------------------


def test_redact_error_masks_secret_lookalikes():
    masked = notif._redact_error("got token=abc123 and key=xyz")
    assert "abc123" not in masked
    assert "xyz" not in masked
    assert "***" in masked


def test_redact_error_passes_none_through():
    assert notif._redact_error(None) is None


# ---------- helpers ----------------------------------------------------------


def _msg(
    *,
    alert_id: str | None = None,
    user_id: str = "local",
    body: str = "test body",
    title: str = "test",
    symbol: str | None = "RELIANCE",
) -> NotificationMessage:
    return NotificationMessage(
        alert_id=alert_id, user_id=user_id, symbol=symbol,
        title=title, body=body, metadata={},
    )
