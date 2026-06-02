"""Secrets-at-rest for the agentX `settings` table (ADR-003).

Plaintext API keys, bot tokens, SMTP passwords, and broker credentials in the
settings table are unsafe for any hosted/multi-user deployment. This module
provides Fernet symmetric encryption applied at the persistence boundary so
the SQLite file can be backed up, copied, or leaked without exposing the
actual secrets.

Threat model
------------
- In scope: SQLite file exfiltration, accidental commits of `stockpilot.db`,
  shared backups, multi-tenant filesystem access.
- Out of scope: a process-level attacker who can read both `AGENTX_SECRETS_KEY`
  (or the on-disk dev key file) AND the SQLite file. If you can read both,
  you can decrypt.

Master key sourcing
-------------------
1. `AGENTX_SECRETS_KEY` env var (preferred — production).
2. DEV mode only (`AGENTX_DEV=1`): auto-generate and persist to
   `~/.agentx/secrets.key` (chmod 600). A loud WARNING is logged on first
   creation; thereafter the key is reused.
3. Otherwise: `SecretsKeyMissing` is raised at startup. Fail closed.

Ciphertext format
-----------------
`enc:v1:<base64-fernet-token>` — the prefix lets us detect already-encrypted
values and migrate idempotently. `is_encrypted()` only inspects the prefix;
it does NOT verify integrity (that happens on `decrypt()`).

Key rotation
------------
1. Decrypt every SECRET_KEYS row with the current key (offline script).
2. Set `AGENTX_SECRETS_KEY` to the new key.
3. Re-run `migrate_plaintext_secrets()` — it will re-seal the now-plaintext
   rows under the new key.
4. Securely destroy the old key.
Fernet supports `MultiFernet` for in-place rotation with overlap; we have
not wired that in yet — add it before rotating in production.

Disaster recovery
-----------------
- Lose `AGENTX_SECRETS_KEY` → all sealed values are unrecoverable. Restore
  from a backup that includes both the DB and the key, or wipe the affected
  settings rows and have the user re-enter them via the UI.
- Always back up the key separately from the database (different storage,
  different access policy).
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


# ── Allowlist of settings whose values must be sealed at rest ───────────────
SECRET_KEYS: frozenset[str] = frozenset({
    "openai_api_key",
    "gemini_api_key",
    "claude_api_key",
    "llm_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    "smtp_password",
    "twilio_auth_token",
    "twilio_account_sid",
    "kite_access_token",
    "kite_api_secret",
    "kite_api_key",
    # AngelOne SmartAPI credentials — all four sealed at rest.
    "angelone_api_key",
    "angelone_client_code",
    "angelone_mpin",
    "angelone_totp_secret",
    # Upstox data-source token (daily OAuth token) + app creds.
    "upstox_access_token",
    "upstox_api_key",
    "upstox_api_secret",
    # Twelve Data keyed fallback.
    "twelvedata_api_key",
    # Financial Modeling Prep (fundamentals + earnings calendar) and
    # Finnhub (macro / forex) — both sealed at rest.
    "fmp_api_key",
    "finnhub_api_key",
    "msg91_auth_key",
    "gupshup_api_key",
})

_CIPHERTEXT_PREFIX = "enc:v1:"
_ENV_KEY = "AGENTX_SECRETS_KEY"
_ENV_DEV_FLAG = "AGENTX_DEV"
_DEV_KEY_PATH = Path.home() / ".agentx" / "secrets.key"


class SecretsKeyMissing(RuntimeError):
    """Raised in non-dev mode when no master key is available.

    The remediation path is in the message — do not swallow this; the app
    should fail to start so the operator notices.
    """


class SecretsManager:
    """Fernet-backed envelope encryption for at-rest secrets.

    One instance per process. Construct via :meth:`from_env` so the master
    key resolution policy stays in one place.
    """

    def __init__(self, key: bytes) -> None:
        """Initialize with a raw 32-byte url-safe base64-encoded Fernet key.

        Args:
            key: A 44-char url-safe base64 Fernet key (as produced by
                ``Fernet.generate_key()``).

        Raises:
            ValueError: If `key` is not a valid Fernet key.
        """
        # Fernet() validates the key shape; we let it raise.
        self._fernet = Fernet(key)

    # ── Construction ────────────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "SecretsManager":
        """Build a manager using the documented key-sourcing policy.

        Returns:
            Configured SecretsManager.

        Raises:
            SecretsKeyMissing: If no key is set and DEV mode is off.
        """
        env_key = os.environ.get(_ENV_KEY, "").strip()
        if env_key:
            return cls(env_key.encode("ascii"))

        if os.environ.get(_ENV_DEV_FLAG, "").strip() == "1":
            return cls(_load_or_create_dev_key())

        raise SecretsKeyMissing(
            f"Missing master key. Set {_ENV_KEY}=$(python -c "
            "'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())') "
            f"in the environment, or set {_ENV_DEV_FLAG}=1 for local dev "
            "(auto-generates a key under ~/.agentx/secrets.key)."
        )

    # ── Core API ────────────────────────────────────────────────────────
    def encrypt(self, plaintext: str) -> str:
        """Seal `plaintext` and return a prefixed ciphertext string.

        Args:
            plaintext: The raw secret value. Must be a `str`; empty strings
                are sealed as well so callers don't need a special case.

        Returns:
            ``enc:v1:<base64-token>``.
        """
        if not isinstance(plaintext, str):
            raise TypeError(f"encrypt requires str, got {type(plaintext).__name__}")
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_CIPHERTEXT_PREFIX}{token}"

    def decrypt(self, ciphertext: str) -> str:
        """Unseal a prefixed ciphertext string.

        Args:
            ciphertext: A value previously returned by :meth:`encrypt`.

        Returns:
            The original plaintext.

        Raises:
            ValueError: If the value is not in the expected ``enc:v1:``
                envelope or fails Fernet integrity verification.
        """
        if not self.is_encrypted(ciphertext):
            raise ValueError("decrypt called on a non-encrypted value")
        token = ciphertext[len(_CIPHERTEXT_PREFIX):].encode("ascii")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Failed to decrypt secret — wrong master key or tampered value"
            ) from exc

    @staticmethod
    def is_encrypted(value: str) -> bool:
        """Return True if `value` carries the at-rest envelope prefix."""
        return isinstance(value, str) and value.startswith(_CIPHERTEXT_PREFIX)

    # ── Aliases for call-site clarity ──────────────────────────────────
    def seal_key(self, key: str) -> str:
        """Idempotent encrypt: passes through values that are already sealed."""
        if self.is_encrypted(key):
            return key
        if key == "":
            # Persist empty marker as plaintext — saves a round-trip and
            # keeps `<key>_configured` checks cheap.
            return ""
        return self.encrypt(key)

    def unseal_key(self, value: str) -> str:
        """Idempotent decrypt: passes through values that are not sealed."""
        if not self.is_encrypted(value):
            return value
        return self.decrypt(value)

    # ── Bulk migration ─────────────────────────────────────────────────
    def migrate_plaintext(
        self,
        rows: Iterable[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Identify SECRET_KEYS rows that are stored in plaintext and seal them.

        Args:
            rows: Iterable of ``(key, value)`` pairs from the settings table.

        Returns:
            The subset that needs an UPDATE: ``(key, sealed_value)`` pairs.
            Rows already encrypted, or with non-secret keys, or empty
            values are skipped.
        """
        updates: list[tuple[str, str]] = []
        for key, value in rows:
            if key not in SECRET_KEYS:
                continue
            if value is None or value == "":
                continue
            if self.is_encrypted(value):
                continue
            updates.append((key, self.encrypt(value)))
        return updates


# ── Module-level singleton ──────────────────────────────────────────────
_manager: SecretsManager | None = None


def get_manager() -> SecretsManager:
    """Lazy-initialised process-wide manager. Never returns None."""
    global _manager
    if _manager is None:
        _manager = SecretsManager.from_env()
    return _manager


def reset_manager_for_tests() -> None:
    """Drop the cached manager. ONLY for tests that mutate env between cases."""
    global _manager
    _manager = None


# ── Dev-key persistence ─────────────────────────────────────────────────
def _load_or_create_dev_key() -> bytes:
    """Read the dev key file; create it on first run with mode 0600.

    Returns:
        The 44-byte url-safe base64 Fernet key.
    """
    path = _DEV_KEY_PATH
    if path.exists():
        key = path.read_bytes().strip()
        # Validate by attempting to construct a Fernet — invalid file should
        # not silently regenerate; that would orphan existing ciphertexts.
        Fernet(key)
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Write atomically: temp file + chmod + rename.
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    tmp.replace(path)
    logger.warning(
        "AGENTX_DEV: generated new master key at %s — back this file up. "
        "Losing it makes all sealed settings unrecoverable.",
        path,
    )
    return key
