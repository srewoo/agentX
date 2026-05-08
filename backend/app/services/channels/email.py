"""SMTP email channel via aiosmtplib.

aiosmtplib is the canonical async SMTP client for asyncio apps. We
deliberately keep the formatting plain-text + minimal headers so we
don't get caught in HTML-in-spam-filters land.
"""
from __future__ import annotations

import logging
import time
from email.message import EmailMessage
from typing import Optional

from app.models.alert import DeliveryResult, NotificationMessage

logger = logging.getLogger(__name__)

# Connect+send timeout. SMTP is slow; 30s is a sane upper bound.
_SMTP_TIMEOUT_SECONDS = 30


class EmailChannel:
    """Send transactional emails over SMTP+STARTTLS."""

    name: str = "email"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addr: str,
        use_tls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_addr = from_addr
        self._to_addr = to_addr
        self._use_tls = use_tls

    def _configured(self) -> bool:
        return bool(self._host and self._port and self._from_addr and self._to_addr)

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        if not self._configured():
            return DeliveryResult(
                channel=self.name, ok=False, status="failed",
                error="client:email not configured (missing host/port/from/to)",
            )

        try:
            import aiosmtplib  # local import — lib only required if email is enabled
        except ImportError:
            return DeliveryResult(
                channel=self.name, ok=False, status="failed",
                error="server:aiosmtplib not installed",
            )

        msg = EmailMessage()
        msg["From"] = self._from_addr
        msg["To"] = self._to_addr
        msg["Subject"] = _subject_for(message)
        msg.set_content(message.body)

        start = time.perf_counter()
        try:
            response = await aiosmtplib.send(
                msg,
                hostname=self._host,
                port=self._port,
                username=self._username or None,
                password=self._password or None,
                start_tls=self._use_tls,
                timeout=_SMTP_TIMEOUT_SECONDS,
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            # aiosmtplib.send returns (errors_dict, response_message). We treat
            # any per-recipient error as a failure to surface it cleanly.
            errors, _ = response if isinstance(response, tuple) else ({}, "")
            if errors:
                return DeliveryResult(
                    channel=self.name, ok=False, status="failed",
                    error=f"server:smtp recipient errors: {list(errors.keys())}",
                    duration_ms=duration_ms,
                )
            return DeliveryResult(
                channel=self.name, ok=True, status="delivered",
                provider_id=msg.get("Message-ID"),
                duration_ms=duration_ms,
            )
        except Exception as e:  # aiosmtplib raises a small zoo of types
            return DeliveryResult(
                channel=self.name, ok=False, status="failed",
                error=_classify_smtp_error(e),
            )

    async def healthcheck(self) -> bool:
        if not self._configured():
            return False
        try:
            import aiosmtplib
        except ImportError:
            return False
        try:
            client = aiosmtplib.SMTP(
                hostname=self._host,
                port=self._port,
                timeout=_SMTP_TIMEOUT_SECONDS,
                start_tls=False,
            )
            await client.connect()
            try:
                if self._use_tls:
                    await client.starttls()
                await client.noop()
            finally:
                try:
                    await client.quit()
                except Exception:
                    pass
            return True
        except Exception:
            return False


def _subject_for(message: NotificationMessage) -> str:
    if message.symbol:
        return f"[agentX] {message.title} — {message.symbol}"
    return f"[agentX] {message.title}"


def _classify_smtp_error(e: Exception) -> str:
    """Tag with client: / server: so the retry layer knows what to do."""
    name = type(e).__name__
    # aiosmtplib auth/recipient/sender errors are 4xx-equivalent.
    if "Auth" in name or "Recipient" in name or "Sender" in name:
        return f"client:smtp {name}"
    return f"server:smtp {name}"


# Public for tests that want to exercise the subject builder.
__all__ = ["EmailChannel"]

# Optional: keep the helper exposed for other modules that want it.
def email_subject_for(message: NotificationMessage) -> str:  # pragma: no cover
    return _subject_for(message)
