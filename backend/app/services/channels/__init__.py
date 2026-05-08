"""Notification channel adapters.

Each module exposes a single adapter class implementing
`NotificationChannel`. The notifications service composes them at runtime
based on the channels list on each Alert.

Adapters MUST:
  - read all secrets from `provider_config` passed at construction time;
  - never log raw secrets (token, password, sid, auth-token);
  - translate transport exceptions into `DeliveryResult(ok=False, ...)`
    instead of bubbling — the routing service decides retry policy;
  - distinguish 4xx (do not retry) from 5xx / network (retry) by setting
    `error` to a string starting with "client:" or "server:" respectively.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.alert import DeliveryResult, NotificationMessage


@runtime_checkable
class NotificationChannel(Protocol):
    """Contract every channel adapter implements."""

    name: str

    async def send(self, message: NotificationMessage) -> DeliveryResult: ...
    async def healthcheck(self) -> bool: ...
