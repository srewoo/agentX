from __future__ import annotations

"""Unit tests for the P0 hardening fixes:

1. `routers/settings.py` must NEVER return raw API key values; instead it
   exposes a `<key>_configured: bool` flag.
2. `main.py` rate-limiter must do prefix+suffix matching so that
   `/api/stocks/{sym}/technicals` does NOT inherit the AI-analysis 15/min
   bucket meant only for `/api/stocks/{sym}/ai-analysis`.
3. `main.py` `_client_ip` must only honour `X-Forwarded-For` when the
   `TRUST_FORWARDED=1` env var is set, and must pick the leftmost public IP.
"""

import importlib
import os
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. Settings secret redaction
# ---------------------------------------------------------------------------

def test_redact_secrets_replaces_keys_with_configured_flags():
    from app.routers.settings import _redact_secrets

    raw = {
        "openai_api_key": "sk-live-DO-NOT-LEAK",
        "gemini_api_key": "",
        "claude_api_key": "   ",  # whitespace-only must count as not configured
        "llm_api_key": "abc",
        "risk_mode": "balanced",
        "alert_interval_minutes": "30",
    }

    out = _redact_secrets(raw)

    # Raw secret values are gone.
    for secret_key in ("openai_api_key", "gemini_api_key", "claude_api_key", "llm_api_key"):
        assert secret_key not in out, f"{secret_key} leaked in response"

    # Boolean flags reflect non-empty / configured state.
    assert out["openai_api_key_configured"] is True
    assert out["gemini_api_key_configured"] is False
    assert out["claude_api_key_configured"] is False
    assert out["llm_api_key_configured"] is True

    # Non-secret settings pass through unchanged.
    assert out["risk_mode"] == "balanced"
    assert out["alert_interval_minutes"] == "30"


def test_redact_secrets_adds_missing_flags_as_false():
    """If the DB has no row for a secret key, the flag must still be present
    as False so the UI can render a consistent shape."""
    from app.routers.settings import _redact_secrets

    out = _redact_secrets({"risk_mode": "aggressive"})
    for secret_key in ("openai_api_key", "gemini_api_key", "claude_api_key", "llm_api_key"):
        assert out[f"{secret_key}_configured"] is False


# ---------------------------------------------------------------------------
# 2. Rate-limit route matching — the original bug
# ---------------------------------------------------------------------------

def test_route_match_does_not_confuse_technicals_with_ai_analysis():
    from app.main import _match_route_limit

    # AI analysis path: 15/min
    bucket, limit, window = _match_route_limit("/api/stocks/RELIANCE/ai-analysis")
    assert bucket == "ai_analysis"
    assert (limit, window) == (15, 60)

    # Technicals path used to incorrectly match the AI-analysis rule
    # because of `"/api/stocks/" in path`. Must now fall through to default.
    bucket, limit, window = _match_route_limit("/api/stocks/RELIANCE/technicals")
    assert bucket == "default", "technicals leaked into ai_analysis bucket"
    assert (limit, window) == (120, 60)

    # Other stock subpaths also fall through to default.
    bucket, _, _ = _match_route_limit("/api/stocks/RELIANCE/quote")
    assert bucket == "default"

    # Backtest prefix still matches.
    bucket, limit, _ = _match_route_limit("/api/backtest/RELIANCE")
    assert bucket == "backtest"
    assert limit == 60

    # Screener and scan trigger still match.
    assert _match_route_limit("/api/screener")[0] == "screener"
    assert _match_route_limit("/api/scan/trigger")[0] == "scan_trigger"


def test_rate_limit_buckets_are_isolated_per_route():
    """Burning the AI-analysis bucket must not block /technicals calls."""
    from app.main import _check_rate_limit, _rate_buckets

    # Clean slate for the test IP.
    for k in list(_rate_buckets.keys()):
        if k.startswith("1.2.3.4:"):
            del _rate_buckets[k]

    ip = "1.2.3.4"
    # Exhaust the 15/min ai-analysis bucket.
    for _ in range(15):
        assert _check_rate_limit(ip, "/api/stocks/RELIANCE/ai-analysis") is True
    assert _check_rate_limit(ip, "/api/stocks/RELIANCE/ai-analysis") is False

    # Technicals must still be allowed — separate bucket, separate limit.
    assert _check_rate_limit(ip, "/api/stocks/RELIANCE/technicals") is True


# ---------------------------------------------------------------------------
# 3. X-Forwarded-For handling
# ---------------------------------------------------------------------------

def _make_request(client_host: str, headers: dict | None = None):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = client_host
    req.headers = headers or {}
    return req


def test_client_ip_ignores_forwarded_header_by_default(monkeypatch):
    # Force-reload main with TRUST_FORWARDED unset so the module-level
    # `_TRUST_FORWARDED` constant reflects the off state.
    monkeypatch.delenv("TRUST_FORWARDED", raising=False)
    import app.main as main_mod
    importlib.reload(main_mod)

    req = _make_request("10.0.0.5", {"x-forwarded-for": "8.8.8.8, 1.2.3.4"})
    assert main_mod._client_ip(req) == "10.0.0.5"


def test_client_ip_uses_leftmost_public_when_trust_forwarded(monkeypatch):
    monkeypatch.setenv("TRUST_FORWARDED", "1")
    import app.main as main_mod
    importlib.reload(main_mod)

    # Leftmost public IP wins; private hops are skipped.
    req = _make_request(
        "10.0.0.5",
        {"x-forwarded-for": "10.0.0.1, 192.168.1.1, 8.8.8.8, 1.2.3.4"},
    )
    assert main_mod._client_ip(req) == "8.8.8.8"

    # If header is empty/missing, fall back to socket peer.
    req2 = _make_request("203.0.113.7", {})
    assert main_mod._client_ip(req2) == "203.0.113.7"

    # Reset for other tests.
    monkeypatch.delenv("TRUST_FORWARDED", raising=False)
    importlib.reload(main_mod)
