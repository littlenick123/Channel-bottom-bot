from __future__ import annotations

import asyncio
import logging
import time

from .pending_drafts import PendingDraftService


logger = logging.getLogger(__name__)


class PendingCleanupLoop:
    def __init__(self, service: PendingDraftService, interval_seconds: int | float) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.service.cleanup_expired(time.time())
            except Exception:
                logger.exception("Failed to clean expired pending drafts")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
