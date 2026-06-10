"""Tests for the /api/settings/test-upstox endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import orchestrator, upstox_fetcher


def test_test_upstox_no_token(monkeypatch):
    async def _no_token():
        return {}
    monkeypatch.setattr(orchestrator, "_get_settings", _no_token)

    client = TestClient(app)
    resp = client.post("/api/settings/test-upstox")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "No Upstox access token" in body["message"]


def test_test_upstox_with_token(monkeypatch):
    async def _with_token():
        return {"upstox_access_token": "tok"}
    monkeypatch.setattr(orchestrator, "_get_settings", _with_token)

    async def _ok(token):
        assert token == "tok"
        return {"ok": True, "message": "Connected as Rohan."}
    monkeypatch.setattr(upstox_fetcher, "test_connection", _ok)

    client = TestClient(app)
    resp = client.post("/api/settings/test-upstox")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "Connected as Rohan."}


# ── OAuth login-url + code-exchange endpoints ────────────────

def test_login_url_requires_api_key(monkeypatch):
    async def _no_key():
        return {}
    monkeypatch.setattr(orchestrator, "_get_settings", _no_key)

    client = TestClient(app)
    resp = client.get("/api/settings/upstox-login-url", params={"redirect_uri": "https://x.test/cb"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "upstox_api_key" in body["message"]


def test_login_url_built_from_stored_key(monkeypatch):
    async def _with_key():
        return {"upstox_api_key": "APIKEY"}
    monkeypatch.setattr(orchestrator, "_get_settings", _with_key)

    client = TestClient(app)
    resp = client.get("/api/settings/upstox-login-url", params={"redirect_uri": "https://x.test/cb"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "client_id=APIKEY" in body["url"]
    assert "redirect_uri=https%3A%2F%2Fx.test%2Fcb" in body["url"]


def test_exchange_code_requires_app_creds(monkeypatch):
    async def _no_creds():
        return {"upstox_api_key": "APIKEY"}  # missing secret
    monkeypatch.setattr(orchestrator, "_get_settings", _no_creds)

    client = TestClient(app)
    resp = client.post(
        "/api/settings/upstox-exchange-code",
        json={"code": "abc", "redirect_uri": "https://x.test/cb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "upstox_api_secret" in body["message"]


def test_exchange_code_saves_token(monkeypatch):
    import aiosqlite as _aiosqlite

    from app.database import DB_PATH
    from app.services import secrets

    # Enable the local-dev master key so seal_key() works without a configured
    # AGENTX_SECRETS_KEY (CI/dev parity).
    monkeypatch.setenv("AGENTX_DEV", "1")
    secrets.reset_manager_for_tests()

    # Ensure the settings table exists on the temp DB this sync test writes to
    # (the async `db` fixture that normally creates it isn't pulled in here).
    import sqlite3
    with sqlite3.connect(DB_PATH) as _c:
        _c.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        _c.commit()

    async def _creds():
        return {"upstox_api_key": "APIKEY", "upstox_api_secret": "SECRET"}
    monkeypatch.setattr(orchestrator, "_get_settings", _creds)

    async def _exchange(code, *, api_key, api_secret, redirect_uri):
        assert code == "the-code"
        assert (api_key, api_secret) == ("APIKEY", "SECRET")
        return {"ok": True, "access_token": "ACCESS123", "message": "Access token generated."}
    monkeypatch.setattr(upstox_fetcher, "exchange_code", _exchange)

    client = TestClient(app)
    resp = client.post(
        "/api/settings/upstox-exchange-code",
        json={"code": "the-code", "redirect_uri": "https://x.test/cb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    # Token was persisted (sealed, so not equal to plaintext) under the key.
    async def _read():
        async with _aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key='upstox_access_token'"
            ) as cur:
                return await cur.fetchone()

    import asyncio
    row = asyncio.run(_read())
    assert row is not None and row[0]
    assert row[0] != "ACCESS123"  # stored sealed, never plaintext
