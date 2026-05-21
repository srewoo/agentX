"""Regression test for the LLM-judge 401 bug.

The orchestrator's ``_get_settings`` is the single entry point that feeds
every in-process consumer (judge, analyst, notifications). It MUST unseal
SECRET_KEYS values before returning, otherwise providers receive the
``enc:v1:...`` ciphertext as their API key and 401.
"""
from __future__ import annotations

import aiosqlite
import pytest

from app.services.orchestrator import _get_settings
from app.services.secrets import SECRET_KEYS, get_manager, reset_manager_for_tests


@pytest.mark.asyncio
async def test_get_settings_unseals_secret_keys(tmp_path, monkeypatch):
    # Force dev mode so the secrets manager can boot without a real env key.
    monkeypatch.setenv("AGENTX_DEV", "1")
    reset_manager_for_tests()
    db_path = tmp_path / "test_settings.db"
    monkeypatch.setattr("app.services.orchestrator.DB_PATH", str(db_path))

    mgr = get_manager()
    plain_key = "sk-test-1234567890ABCDEFGHIJ"
    sealed = mgr.seal_key(plain_key)
    assert sealed != plain_key, "test prerequisite: secrets manager must encrypt"
    assert "gemini_api_key" in SECRET_KEYS, "gemini_api_key should be a secret"

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("gemini_api_key", sealed),
        )
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("risk_mode", "balanced"),
        )
        await db.commit()

    settings = await _get_settings()
    # Secret keys unsealed — providers get plaintext.
    assert settings["gemini_api_key"] == plain_key
    # Non-secret keys pass through verbatim.
    assert settings["risk_mode"] == "balanced"


@pytest.mark.asyncio
async def test_get_settings_tolerates_bad_ciphertext(tmp_path, monkeypatch):
    """A corrupted ciphertext must not crash the whole scan cycle."""
    db_path = tmp_path / "test_settings.db"
    monkeypatch.setattr("app.services.orchestrator.DB_PATH", str(db_path))

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # Ciphertext-shaped but un-decryptable
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("openai_api_key", "enc:v1:garbage_that_will_not_decrypt"),
        )
        await db.commit()

    # Should not raise; the value falls back to its ciphertext form so the
    # downstream LLM call will 401, but the scan cycle survives.
    settings = await _get_settings()
    assert "openai_api_key" in settings
