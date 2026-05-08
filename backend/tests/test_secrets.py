"""Tests for the secrets-at-rest layer (ADR-003).

Covers:
- Fernet round-trip and prefix detection
- Master key sourcing (env, dev mode, prod-missing)
- Idempotent plaintext migration
- Settings GET still redacts to `_configured` after sealing
- Consumer-style read returns plaintext via get_setting()
"""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite
import pytest
from cryptography.fernet import Fernet


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_master_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set AGENTX_SECRETS_KEY to a fresh Fernet key for the duration of a test."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("AGENTX_SECRETS_KEY", key)
    monkeypatch.delenv("AGENTX_DEV", raising=False)

    # Reset module-level singleton between tests so env changes take effect.
    from app.services import secrets as secrets_mod
    secrets_mod.reset_manager_for_tests()
    yield key
    secrets_mod.reset_manager_for_tests()


@pytest.fixture
def dev_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Configure dev-mode key sourcing into an isolated tmp HOME."""
    monkeypatch.delenv("AGENTX_SECRETS_KEY", raising=False)
    monkeypatch.setenv("AGENTX_DEV", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    # Re-import resets the cached module-level _DEV_KEY_PATH (it bound at
    # import). Patch directly to make the test deterministic.
    from app.services import secrets as secrets_mod
    monkeypatch.setattr(
        secrets_mod, "_DEV_KEY_PATH", tmp_path / ".agentx" / "secrets.key"
    )
    secrets_mod.reset_manager_for_tests()
    yield tmp_path / ".agentx" / "secrets.key"
    secrets_mod.reset_manager_for_tests()


@pytest.fixture
def prod_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip both env knobs so SecretsKeyMissing is raised."""
    monkeypatch.delenv("AGENTX_SECRETS_KEY", raising=False)
    monkeypatch.delenv("AGENTX_DEV", raising=False)
    from app.services import secrets as secrets_mod
    secrets_mod.reset_manager_for_tests()
    yield
    secrets_mod.reset_manager_for_tests()


# ── Round-trip and detection ────────────────────────────────────────────

class TestEncryptDecrypt:
    """Core Fernet envelope behaviour."""

    def test_round_trip_preserves_value(self, fresh_master_key: str) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        plaintext = "sk-live-abc-123-XYZ"
        sealed = mgr.encrypt(plaintext)

        assert sealed != plaintext
        assert sealed.startswith("enc:v1:")
        assert mgr.decrypt(sealed) == plaintext

    def test_is_encrypted_detects_prefix(self, fresh_master_key: str) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        assert mgr.is_encrypted(mgr.encrypt("hello")) is True
        assert mgr.is_encrypted("hello") is False
        assert mgr.is_encrypted("") is False
        assert mgr.is_encrypted("enc:v2:other") is False  # version mismatch

    def test_seal_key_is_idempotent(self, fresh_master_key: str) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        once = mgr.seal_key("token")
        twice = mgr.seal_key(once)
        assert once == twice
        assert mgr.unseal_key(twice) == "token"

    def test_seal_key_passes_empty_through(self, fresh_master_key: str) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        assert mgr.seal_key("") == ""
        assert mgr.unseal_key("") == ""

    def test_decrypt_rejects_plaintext(self, fresh_master_key: str) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        with pytest.raises(ValueError):
            mgr.decrypt("not-encrypted")

    def test_decrypt_rejects_tampered(self, fresh_master_key: str) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        sealed = mgr.encrypt("payload")
        tampered = sealed[:-2] + "AA"
        with pytest.raises(ValueError):
            mgr.decrypt(tampered)


# ── Key sourcing ────────────────────────────────────────────────────────

class TestKeySourcing:
    """How the master key is discovered."""

    def test_prod_missing_key_raises(self, prod_no_key: None) -> None:
        from app.services.secrets import SecretsKeyMissing, SecretsManager

        with pytest.raises(SecretsKeyMissing) as exc_info:
            SecretsManager.from_env()
        # Remediation must be in the message — operator must see how to fix.
        assert "AGENTX_SECRETS_KEY" in str(exc_info.value)

    def test_dev_mode_creates_key_file_with_safe_perms(
        self,
        dev_mode: Path,
    ) -> None:
        from app.services.secrets import SecretsManager

        assert not dev_mode.exists()
        mgr = SecretsManager.from_env()
        assert dev_mode.exists()
        # 0600
        mode = dev_mode.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600 perms, got {oct(mode)}"
        # Reusable
        round_tripped = mgr.decrypt(mgr.encrypt("x"))
        assert round_tripped == "x"

    def test_dev_mode_persists_key_across_calls(self, dev_mode: Path) -> None:
        from app.services import secrets as secrets_mod
        from app.services.secrets import SecretsManager

        mgr1 = SecretsManager.from_env()
        sealed = mgr1.encrypt("durable")

        # Reset singleton — but key file remains; second manager must decrypt.
        secrets_mod.reset_manager_for_tests()
        mgr2 = SecretsManager.from_env()
        assert mgr2.decrypt(sealed) == "durable"


# ── Migration ───────────────────────────────────────────────────────────

class TestMigration:
    """Plaintext rows get sealed; sealed rows are left alone."""

    def test_migrate_plaintext_returns_only_secret_keys(
        self,
        fresh_master_key: str,
    ) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        rows = [
            ("openai_api_key", "sk-plaintext"),
            ("risk_mode", "balanced"),  # not a secret key
            ("telegram_bot_token", "bot-plain"),
            ("twilio_auth_token", ""),  # empty — skip
            ("gemini_api_key", mgr.encrypt("already-sealed")),  # skip
        ]
        updates = dict(mgr.migrate_plaintext(rows))

        assert set(updates.keys()) == {"openai_api_key", "telegram_bot_token"}
        assert mgr.decrypt(updates["openai_api_key"]) == "sk-plaintext"

    def test_migrate_plaintext_idempotent(self, fresh_master_key: str) -> None:
        from app.services.secrets import SecretsManager

        mgr = SecretsManager.from_env()
        rows = [("openai_api_key", "sk-1")]

        first = mgr.migrate_plaintext(rows)
        assert len(first) == 1

        # Apply, then re-migrate — should produce no work.
        sealed_rows = [(first[0][0], first[0][1])]
        second = mgr.migrate_plaintext(sealed_rows)
        assert second == []


# ── End-to-end via database.py + settings table ────────────────────────

@pytest.mark.asyncio
class TestDatabaseIntegration:
    """get_setting / migrate_plaintext_secrets against a real SQLite DB."""

    async def test_migrate_plaintext_secrets_seals_in_place(
        self,
        fresh_master_key: str,
        db: aiosqlite.Connection,
        tmp_db_path: str,
    ) -> None:
        # Seed plaintext secret directly
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("openai_api_key", "sk-secret-XXX"),
        )
        await db.commit()

        from app.database import (
            get_setting,
            get_setting_raw,
            migrate_plaintext_secrets,
        )

        sealed_count = await migrate_plaintext_secrets()
        assert sealed_count == 1

        raw = await get_setting_raw("openai_api_key")
        assert raw is not None and raw.startswith("enc:v1:")

        # Consumer view: transparent decryption.
        plaintext = await get_setting("openai_api_key")
        assert plaintext == "sk-secret-XXX"

        # Idempotent on second run.
        again = await migrate_plaintext_secrets()
        assert again == 0

    async def test_get_setting_passes_through_non_secret(
        self,
        fresh_master_key: str,
        db: aiosqlite.Connection,
    ) -> None:
        from app.database import get_setting

        value = await get_setting("risk_mode")
        assert value == "balanced"

    async def test_get_setting_returns_none_for_missing(
        self,
        fresh_master_key: str,
        db: aiosqlite.Connection,
    ) -> None:
        from app.database import get_setting

        assert await get_setting("nonexistent_key") is None


# ── Settings router GET still redacts ───────────────────────────────────

@pytest.mark.asyncio
class TestSettingsRouterRedaction:
    """The public GET endpoint must never return ciphertext or plaintext."""

    async def test_get_returns_configured_flag_not_ciphertext(
        self,
        fresh_master_key: str,
        db: aiosqlite.Connection,
    ) -> None:
        from app.services.secrets import get_manager

        sealed = get_manager().seal_key("sk-real")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("openai_api_key", sealed),
        )
        await db.commit()

        from fastapi.testclient import TestClient

        # Import lazily — instantiating the app triggers heavy imports
        # that are fine but we want them after env is set.
        from app.routers.settings import router as settings_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(settings_router)
        client = TestClient(app)

        resp = client.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()["settings"]

        # Ciphertext must not leak.
        assert "openai_api_key" not in body
        assert body.get("openai_api_key_configured") is True
        # Spot-check — no enc: anywhere in response
        assert "enc:v1:" not in resp.text


# ── llm_client integration: stored sealed → consumer reads plaintext ───

@pytest.mark.asyncio
class TestLLMClientIntegration:
    """A sealed key in settings must surface as plaintext to consumers via get_setting."""

    async def test_sealed_key_surfaces_as_plaintext(
        self,
        fresh_master_key: str,
        db: aiosqlite.Connection,
    ) -> None:
        from app.database import get_setting
        from app.services.secrets import get_manager

        sealed = get_manager().seal_key("gemini-XYZ")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("gemini_api_key", sealed),
        )
        await db.commit()

        plaintext = await get_setting("gemini_api_key")
        assert plaintext == "gemini-XYZ"


# ── Bulk consumers: analysis router & notifications provider config ────

@pytest.mark.asyncio
class TestBulkConsumerDecryption:
    """Consumers that bulk-load the settings table must receive plaintext
    for SECRET_KEYS, never the `enc:v1:...` ciphertext."""

    async def test_analysis_router_get_settings_decrypts_secret_keys(
        self,
        fresh_master_key: str,
        db: aiosqlite.Connection,
    ) -> None:
        """Analysis route loads settings as a dict and hands them to
        llm_analyst, which expects plaintext API keys."""
        from app.routers.analysis import _get_settings
        from app.services.secrets import SECRET_KEYS, get_manager

        # Clear any sealed-by-prior-test leftovers — the conftest `db`
        # fixture doesn't truncate the settings table between tests, and
        # the master key is fresh per test so stale ciphertext would fail
        # to decrypt under our key.
        for k in SECRET_KEYS:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, ""),
            )

        mgr = get_manager()
        sealed_openai = mgr.seal_key("sk-openai-plaintext")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("openai_api_key", sealed_openai),
        )
        # Non-secret key untouched.
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("risk_mode", "balanced"),
        )
        await db.commit()

        settings_map = await _get_settings()
        assert settings_map["openai_api_key"] == "sk-openai-plaintext"
        assert settings_map["risk_mode"] == "balanced"
        assert "enc:v1:" not in settings_map["openai_api_key"]

    async def test_notifications_load_provider_config_decrypts_channel_secrets(
        self,
        fresh_master_key: str,
        db: aiosqlite.Connection,
    ) -> None:
        """Notification channels (telegram, smtp, twilio, msg91, gupshup)
        consume `provider_config` dicts at construction time and need the
        raw token to authenticate upstream."""
        from app.services.notifications import load_provider_config
        from app.services.secrets import SECRET_KEYS, get_manager

        for k in SECRET_KEYS:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, ""),
            )

        mgr = get_manager()
        secret_pairs = {
            "telegram_bot_token": "bot-12345",
            "smtp_password": "smtp-pwd",
            "twilio_auth_token": "tw-auth",
            "msg91_auth_key": "msg91-key",
            "gupshup_api_key": "gup-key",
        }
        for k, v in secret_pairs.items():
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, mgr.seal_key(v)),
            )
        # Throw a non-secret config in too — provider selection knobs are
        # plaintext (not in SECRET_KEYS) and must pass through.
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("sms_provider", "msg91"),
        )
        await db.commit()

        cfg = await load_provider_config()
        for k, expected in secret_pairs.items():
            assert cfg[k] == expected, f"{k} not decrypted"
            assert "enc:v1:" not in cfg[k]
        assert cfg["sms_provider"] == "msg91"


# ── Sync shim ──────────────────────────────────────────────────────────

class TestSyncShim:
    """`get_setting_sync` is the bridge for legacy sync call sites."""

    def test_sync_get_setting_returns_plaintext_for_sealed_key(
        self,
        fresh_master_key: str,
        tmp_db_path: str,
    ) -> None:
        """Outside an event loop, the sync shim should round-trip a sealed
        secret to plaintext."""
        import asyncio as _asyncio

        from app.database import get_setting_sync
        from app.services.secrets import get_manager

        async def _seed():
            import aiosqlite as _sql
            from app.services.secrets import SECRET_KEYS as _SK

            async with _sql.connect(tmp_db_path) as db:
                for k in _SK:
                    await db.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (k, ""),
                    )
                await db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    ("claude_api_key", get_manager().seal_key("anthropic-XYZ")),
                )
                await db.commit()

        _asyncio.run(_seed())

        try:
            assert get_setting_sync("claude_api_key") == "anthropic-XYZ"
        finally:
            _asyncio.run(_cleanup_key(tmp_db_path, "claude_api_key"))

    def test_sync_get_setting_raises_inside_running_loop(
        self,
        fresh_master_key: str,
    ) -> None:
        """Calling the sync shim from inside a running loop must raise —
        we won't silently `asyncio.run` and deadlock."""
        import asyncio as _asyncio

        from app.database import get_setting_sync

        async def _inner():
            with pytest.raises(RuntimeError, match="running event loop"):
                get_setting_sync("openai_api_key")

        _asyncio.run(_inner())


async def _cleanup_key(db_path: str, key: str) -> None:
    import aiosqlite as _sql

    async with _sql.connect(db_path) as db:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await db.commit()
