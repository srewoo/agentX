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
