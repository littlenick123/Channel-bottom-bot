from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Protocol

from .publisher import FloodWaitSignal, RefreshOutcome
from .repositories import Repository


BACKOFF_SECONDS = (5, 15, 60, 300, 900)


class RefreshPublisher(Protocol):
    async def refresh(self, channel_id: int) -> RefreshOutcome: ...


class AdminNotifier(Protocol):
    async def notify_channel_admins(self, channel_id: int, text: str) -> None: ...


class RefreshScheduler:
    def __init__(
        self,
        repository: Repository,
        publisher: RefreshPublisher,
        *,
        clock: Callable[[], float] = time.time,
        notifier: AdminNotifier | None = None,
    ) -> None:
        self.repository = repository
        self.publisher = publisher
        self.clock = clock
        self.notifier = notifier
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._wake = asyncio.Event()
        self._stopping = False

    async def request(self, channel_id: int, reason: str, delay_seconds: int | float) -> None:
        await self.repository.schedule_refresh(channel_id, self.clock() + max(0, delay_seconds), reason)
        self._wake.set()

    async def run_due_once(self) -> None:
        jobs = await self.repository.list_due_refresh_jobs(self.clock())
        if jobs:
            await asyncio.gather(*(self._execute(job) for job in jobs))

    async def _execute(self, job) -> None:
        async with self._locks[job.channel_id]:
            current = await self.repository.get_refresh_job(job.channel_id)
            if not current or current.generation != job.generation or current.due_at > self.clock():
                return
            try:
                outcome = await self.publisher.refresh(job.channel_id)
            except FloodWaitSignal as exc:
                await self.repository.retry_refresh(
                    job.channel_id,
                    job.generation,
                    self.clock() + exc.seconds,
                    str(exc),
                    increment_attempts=False,
                )
                return
            if outcome in {RefreshOutcome.SUCCESS, RefreshOutcome.SKIPPED}:
                await self.repository.complete_refresh(job.channel_id, job.generation)
                return
            if outcome is RefreshOutcome.PAUSED:
                await self.repository.pause_channel(job.channel_id, "publishing requires administrator action")
                await self._notify_paused(job.channel_id, "频道/超级群组发布权限或存储配置不可用，自动置底已暂停。")
                return
            attempt = job.attempts + 1
            if attempt >= len(BACKOFF_SECONDS):
                await self.repository.pause_channel(job.channel_id, "publishing failed five times")
                await self._notify_paused(job.channel_id, "频道/超级群组连续发布失败五次，自动置底已暂停。")
                return
            await self.repository.retry_refresh(
                job.channel_id,
                job.generation,
                self.clock() + BACKOFF_SECONDS[job.attempts],
                "transient publishing error",
                increment_attempts=True,
            )

    async def _notify_paused(self, channel_id: int, text: str) -> None:
        if self.notifier is not None:
            await self.notifier.notify_channel_admins(channel_id, text)

    async def run_forever(self) -> None:
        self._stopping = False
        while not self._stopping:
            await self.run_due_once()
            next_due = await self.repository.next_refresh_due_at()
            timeout = 60.0 if next_due is None else max(0.05, next_due - self.clock())
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
