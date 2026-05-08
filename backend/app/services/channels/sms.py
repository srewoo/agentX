"""SMS channel.

Default provider: Twilio. Switch to MSG91 (popular in India) by setting
`provider_config["sms_provider"] = "msg91"`. Both providers translate
4xx → no retry, 5xx/network → retry, via the "client:" / "server:" tag
on `error`.
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
_MSG91_API = "https://control.msg91.com/api/v5/flow/"


class SmsChannel:
    name: str = "sms"

    def __init__(self, **config: Any) -> None:
        self._provider = (config.get("sms_provider") or "twilio").lower()
        self._config = config

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        if self._provider == "msg91":
            return await _send_msg91(self._config, message)
        return await _send_twilio_sms(self._config, message)

    async def healthcheck(self) -> bool:
        if self._provider == "msg91":
            return bool(
                self._config.get("msg91_auth_key")
                and self._config.get("msg91_flow_id")
                and self._config.get("msg91_mobile")
            )
        return bool(
            self._config.get("twilio_account_sid")
            and self._config.get("twilio_auth_token")
            and self._config.get("twilio_sms_from")
            and self._config.get("twilio_sms_to")
        )


async def _send_twilio_sms(
    cfg: dict[str, Any], message: NotificationMessage
) -> DeliveryResult:
    sid = cfg.get("twilio_account_sid")
    token = cfg.get("twilio_auth_token")
    from_addr = cfg.get("twilio_sms_from")
    to_addr = cfg.get("twilio_sms_to")
    if not all([sid, token, from_addr, to_addr]):
        return DeliveryResult(channel="sms", ok=False, status="failed",
                              error="client:twilio sms not configured")
    url = _TWILIO_API.format(sid=sid)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    body = {"From": from_addr, "To": to_addr, "Body": _format_body(message)}
    headers = {"Authorization": f"Basic {auth}"}
    return await _post(url, data=body, headers=headers, json_body=None, channel="sms")


async def _send_msg91(
    cfg: dict[str, Any], message: NotificationMessage
) -> DeliveryResult:
    auth_key = cfg.get("msg91_auth_key")
    flow_id = cfg.get("msg91_flow_id")
    mobiles = cfg.get("msg91_mobile")
    if not all([auth_key, flow_id, mobiles]):
        return DeliveryResult(channel="sms", ok=False, status="failed",
                              error="client:msg91 not configured")
    headers = {"authkey": auth_key, "Content-Type": "application/json"}
    body = {
        "flow_id": flow_id,
        "mobiles": mobiles,
        # MSG91 flow templates use named variables; we pass title+body so a
        # simple flow can render `##title##` / `##body##`.
        "title": message.title,
        "body": message.body,
    }
    return await _post(_MSG91_API, data=None, headers=headers, json_body=body, channel="sms")


def _format_body(msg: NotificationMessage) -> str:
    sym = f" [{msg.symbol}]" if msg.symbol else ""
    return f"{msg.title}{sym}: {msg.body}"


async def _post(
    url: str,
    data: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    headers: dict[str, str],
    channel: str,
) -> DeliveryResult:
    start = time.perf_counter()
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=data, json=json_body, headers=headers) as resp:
                payload = await _safe_json(resp)
                duration_ms = int((time.perf_counter() - start) * 1000)
                if 200 <= resp.status < 300:
                    return DeliveryResult(
                        channel=channel, ok=True, status="delivered",
                        provider_id=str(
                            payload.get("sid") or payload.get("request_id") or ""
                        ) or None,
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
