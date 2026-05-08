"""Telegram bot channel.

Re-implementation of the client-side Telegram logic that previously lived
in `extension/src/background/service-worker.ts`. The bot token comes from
the secrets store / env / encrypted settings row — never from the request
body, and never echoed back in logs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from app.models.alert import DeliveryResult, NotificationMessage

logger = logging.getLogger(__name__)

# Telegram considers 4xx (bad token, blocked, chat-not-found) terminal.
# Anything 5xx or transport-level we treat as transient and let the
# routing layer retry.
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SECONDS = 10


class TelegramChannel:
    """Sends Markdown messages via the Telegram Bot API."""

    name: str = "telegram"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            # We don't raise — let healthcheck fail and let `send` short-circuit.
            # Avoids crashing app startup if a single channel is misconfigured.
            logger.warning("telegram channel missing bot_token or chat_id; will refuse to send")
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        if not self._bot_token or not self._chat_id:
            return DeliveryResult(
                channel=self.name,
                ok=False,
                status="failed",
                error="client:telegram not configured (missing bot_token/chat_id)",
            )

        text = self._format(message)
        url = _TELEGRAM_API.format(token=quote(self._bot_token, safe=""))
        body = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        start = time.perf_counter()
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body) as resp:
                    payload: dict[str, Any] = await _safe_json(resp)
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    if resp.status >= 200 and resp.status < 300 and payload.get("ok"):
                        provider_id = str(
                            (payload.get("result") or {}).get("message_id") or ""
                        ) or None
                        return DeliveryResult(
                            channel=self.name,
                            ok=True,
                            status="delivered",
                            provider_id=provider_id,
                            duration_ms=duration_ms,
                        )

                    # Classify so the routing layer doesn't retry 4xx.
                    err_desc = payload.get("description") or f"HTTP {resp.status}"
                    prefix = "client:" if 400 <= resp.status < 500 else "server:"
                    return DeliveryResult(
                        channel=self.name,
                        ok=False,
                        status="failed",
                        error=f"{prefix}{err_desc}",
                        duration_ms=duration_ms,
                    )
        except asyncio.TimeoutError:
            return DeliveryResult(
                channel=self.name, ok=False, status="failed",
                error="server:telegram request timed out",
            )
        except aiohttp.ClientError as e:
            return DeliveryResult(
                channel=self.name, ok=False, status="failed",
                error=f"server:telegram transport error: {type(e).__name__}",
            )

    async def healthcheck(self) -> bool:
        """Verify token validity via getMe. Cheap, no message side-effect."""
        if not self._bot_token:
            return False
        url = f"https://api.telegram.org/bot{quote(self._bot_token, safe='')}/getMe"
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    payload = await _safe_json(resp)
                    return bool(resp.status == 200 and payload.get("ok"))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    @staticmethod
    def _format(msg: NotificationMessage) -> str:
        # Markdown-safe-ish — keep escaping minimal; Telegram's Markdown
        # parser tolerates most ascii punctuation in these contexts.
        symbol_line = f"*{msg.symbol}*\n" if msg.symbol else ""
        return f"*{msg.title}*\n{symbol_line}{msg.body}"


async def _safe_json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        return await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {}
