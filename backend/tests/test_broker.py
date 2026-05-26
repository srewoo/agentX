"""Tests for the broker abstraction layer.

We test the factory + credential-detection paths. The actual SDK calls
are integration-only (require real broker credentials and live network),
so they're explicitly skipped here.
"""
from __future__ import annotations

import pytest

from app.services.broker import (
    AngelOneClient,
    KiteClient,
    get_broker_client,
)


# ── Factory routing ──────────────────────────────────────────────────────

def test_factory_returns_none_when_no_broker_selected():
    assert get_broker_client({}) is None
    assert get_broker_client({"broker": ""}) is None


def test_factory_returns_none_for_angelone_without_credentials():
    assert get_broker_client({"broker": "angelone"}) is None


def test_factory_returns_none_for_kite_without_credentials():
    assert get_broker_client({"broker": "kite"}) is None


def test_factory_returns_angelone_when_fully_configured():
    settings = {
        "broker": "angelone",
        "angelone_api_key": "k",
        "angelone_client_code": "A12345",
        "angelone_mpin": "1234",
        "angelone_totp_secret": "JBSWY3DPEHPK3PXP",
    }
    client = get_broker_client(settings)
    assert isinstance(client, AngelOneClient)
    assert client.name == "angelone"


def test_factory_returns_kite_when_minimum_configured():
    settings = {
        "broker": "kite",
        "kite_api_key": "k",
        "kite_access_token": "t",
    }
    client = get_broker_client(settings)
    assert isinstance(client, KiteClient)
    assert client.name == "kite"


def test_factory_ignores_unknown_broker():
    assert get_broker_client({"broker": "robinhood"}) is None


# ── Credential detection ─────────────────────────────────────────────────

def test_angelone_partial_credentials_not_detected():
    """Missing TOTP secret → _has_credentials returns False."""
    client = AngelOneClient({
        "angelone_api_key": "k",
        "angelone_client_code": "A12345",
        "angelone_mpin": "1234",
        # totp_secret missing
    })
    assert client._has_credentials() is False


def test_kite_requires_access_token():
    """API key alone isn't enough — access_token is the daily refresh."""
    client = KiteClient({"kite_api_key": "k"})
    assert client._has_credentials() is False


# ── Login fails gracefully without SDK installed ─────────────────────────

@pytest.mark.asyncio
async def test_angelone_login_returns_false_without_sdk():
    """Missing smartapi-python should not crash — login returns False."""
    client = AngelOneClient({
        "angelone_api_key": "k",
        "angelone_client_code": "A12345",
        "angelone_mpin": "1234",
        "angelone_totp_secret": "JBSWY3DPEHPK3PXP",
    })
    # In test env smartapi-python is NOT installed → ImportError path.
    result = await client.login()
    assert result is False


@pytest.mark.asyncio
async def test_kite_login_returns_false_without_sdk():
    client = KiteClient({"kite_api_key": "k", "kite_access_token": "t"})
    result = await client.login()
    # If kiteconnect isn't installed (test env) → ImportError → False.
    # If it IS installed, the call will fail auth → also False.
    assert result is False
