"""Push notifications — placeholder.

Will become Web Push (VAPID) and/or FCM. For now this short-circuits to
`status=skipped` so the rest of the routing pipeline can be wired up,
tested, and shipped without blocking on push integration.
"""
from __future__ import annotations

import logging

from app.models.alert import DeliveryResult, NotificationMessage

logger = logging.getLogger(__name__)


class PushChannel:
    name: str = "push"

    def __init__(self, **_config: object) -> None:
        # Nothing configured yet — accept and ignore everything.
        pass

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        # Don't fail loudly — this is an intentional no-op pending
        # provider selection. The routing layer treats `skipped` as
        # "not delivered, do not retry, do not alert".
        logger.debug(
            "push channel skip", extra={"alert_id": message.alert_id, "symbol": message.symbol}
        )
        return DeliveryResult(
            channel=self.name, ok=False, status="skipped",
            error="client:push channel not yet implemented",
        )

    async def healthcheck(self) -> bool:
        return False
