"""WhatsApp channel.

Default provider: Twilio Sandbox (whatsapp:+14155238886). Abstracted so
we can later swap to Gupshup (popular in India) or Meta Cloud API
without touching `services.notifications`.

Provider selection is by `provider_config["whatsapp_provider"]`:
  - "twilio"  (default) → uses Twilio sandbox / production sender.
  - "gupshup"           → calls Gupshup HTTP API.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import aiohttp

from app.models.alert import DeliveryResult, NotificationMessage

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_GUPSHUP_API = "https://api.gupshup.io/sm/api/v1/msg"


class WhatsAppChannel:
    """Routes to a provider implementation behind a common interface."""

    name: str = "whatsapp"

    def __init__(self, **config: Any) -> None:
        self._provider = (config.get("whatsapp_provider") or "twilio").lower()
        self._config = config

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        if self._provider == "gupshup":
            return await _send_gupshup(self._config, message)
        return await _send_twilio_whatsapp(self._config, message)

    async def healthcheck(self) -> bool:
        # We don't ping providers — that costs money / quota. We only
        # check the config is structurally complete.
        if self._provider == "gupshup":
            return bool(self._config.get("gupshup_api_key") and self._config.get("gupshup_source"))
        return bool(
            self._config.get("twilio_account_sid")
            and self._config.get("twilio_auth_token")
            and self._config.get("twilio_whatsapp_from")
            and self._config.get("twilio_whatsapp_to")
        )


async def _send_twilio_whatsapp(
    cfg: dict[str, Any], message: NotificationMessage
) -> DeliveryResult:
    sid = cfg.get("twilio_account_sid")
    token = cfg.get("twilio_auth_token")
    from_addr = cfg.get("twilio_whatsapp_from")  # e.g. "whatsapp:+14155238886"
    to_addr = cfg.get("twilio_whatsapp_to")
    if not all([sid, token, from_addr, to_addr]):
        return DeliveryResult(
            channel="whatsapp", ok=False, status="failed",
            error="client:twilio whatsapp not configured",
        )

    url = _TWILIO_API.format(sid=sid)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    body = {"From": from_addr, "To": to_addr, "Body": _format_body(message)}
    headers = {"Authorization": f"Basic {auth}"}
    return await _post_form(url, body, headers, channel="whatsapp")


async def _send_gupshup(
    cfg: dict[str, Any], message: NotificationMessage
) -> DeliveryResult:
    api_key = cfg.get("gupshup_api_key")
    source = cfg.get("gupshup_source")
    destination = cfg.get("gupshup_destination")
    src_name = cfg.get("gupshup_src_name") or "agentX"
    if not all([api_key, source, destination]):
        return DeliveryResult(
            channel="whatsapp", ok=False, status="failed",
            error="client:gupshup not configured",
        )
    headers = {"apikey": api_key, "Content-Type": "application/x-www-form-urlencoded"}
    body = {
        "channel": "whatsapp",
        "source": source,
        "destination": destination,
        "src.name": src_name,
        "message": _format_body(message),
    }
    return await _post_form(_GUPSHUP_API, body, headers, channel="whatsapp")


def _format_body(msg: NotificationMessage) -> str:
    sym = f" [{msg.symbol}]" if msg.symbol else ""
    return f"{msg.title}{sym}\n{msg.body}"


async def _post_form(
    url: str, body: dict[str, Any], headers: dict[str, str], channel: str
) -> DeliveryResult:
    start = time.perf_counter()
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body, headers=headers) as resp:
                payload = await _safe_json(resp)
                duration_ms = int((time.perf_counter() - start) * 1000)
                if 200 <= resp.status < 300:
                    return DeliveryResult(
                        channel=channel, ok=True, status="delivered",
                        provider_id=str(payload.get("sid") or payload.get("messageId") or "") or None,
                        duration_ms=duration_ms,
                    )
                prefix = "client:" if 400 <= resp.status < 500 else "server:"
                err_msg = payload.get("message") or payload.get("error") or f"HTTP {resp.status}"
                return DeliveryResult(
                    channel=channel, ok=False, status="failed",
                    error=f"{prefix}{err_msg}",
                    duration_ms=duration_ms,
                )
    except asyncio.TimeoutError:
        return DeliveryResult(channel=channel, ok=False, status="failed",
                              error="server:request timed out")
    except aiohttp.ClientError as e:
        return DeliveryResult(channel=channel, ok=False, status="failed",
                              error=f"server:transport error: {type(e).__name__}")


async def _safe_json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        return await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {}
