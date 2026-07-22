from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, time as clock_time
from typing import Protocol
from zoneinfo import ZoneInfo

from .analytics import AnalyticsService
from .permissions import PermissionDenied, PermissionUnavailable
from .publisher import FloodWaitSignal, RefreshOutcome
from .repositories import Repository
from .stats import format_chat_report, split_chat_reports


BACKOFF_SECONDS = (5, 15, 60, 300, 900)


class RefreshPublisher(Protocol):
    async def refresh(self, channel_id: int) -> RefreshOutcome: ...


class AdminNotifier(Protocol):
    async def notify_channel_admins(self, channel_id: int, text: str) -> None: ...


class PrivateDeliveryError(RuntimeError):
    """A private chat cannot accept reports until the user changes Telegram settings."""


class DailyReportDeliveryGateway(Protocol):
    async def send_private_text(self, user_id: int, text: str) -> None: ...


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


DAILY_REPORT_RETRY_SECONDS = (60, 300, 900, 3600)


class DailyStatsScheduler:
    """Schedules one consolidated, permission-checked daily report for each manager."""

    def __init__(
        self,
        repository: Repository,
        analytics: AnalyticsService,
        permissions,
        delivery_gateway: DailyReportDeliveryGateway,
        *,
        timezone: ZoneInfo | str = "Asia/Shanghai",
        push_time: clock_time = clock_time(0, 5),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.analytics = analytics
        self.permissions = permissions
        self.delivery_gateway = delivery_gateway
        self.timezone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        self.push_time = push_time
        self.clock = clock
        self._wake = asyncio.Event()
        self._stopping = False

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.timezone)

    def report_cutoff(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=ZoneInfo("UTC"))
        local = instant.astimezone(self.timezone)
        return datetime.combine(local.date(), self.push_time, tzinfo=self.timezone)

    @staticmethod
    def _utc_db_timestamp(instant: datetime) -> str:
        return instant.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

    async def run_due_once(self, instant: datetime | None = None) -> None:
        now = instant.astimezone(self.timezone) if instant is not None else self._now()
        now_timestamp = now.timestamp()
        await self.analytics.heartbeat(now)
        await self.analytics.cleanup_processed_updates(now)
        cutoff = self.report_cutoff(now)
        if now >= cutoff:
            report_date = cutoff.date().isoformat()
            cutoff_utc = self._utc_db_timestamp(cutoff)
            for user_id in await self.repository.list_daily_report_manager_ids(cutoff_utc):
                await self.repository.reserve_daily_report_delivery(user_id, report_date, cutoff.timestamp())
        deliveries = await self.repository.list_due_daily_report_deliveries(now_timestamp)
        for delivery in deliveries:
            await self._deliver(delivery, now)

    async def _deliver(self, delivery, now: datetime) -> None:
        claimed = await self.repository.claim_daily_report_delivery(delivery.user_id, delivery.report_date, now.timestamp())
        if claimed is None:
            return
        payload = await self._payload_for_delivery(claimed, now)
        if payload is None:
            return
        try:
            for index, text in enumerate(payload["chunks"][claimed.next_chunk_index :], start=claimed.next_chunk_index):
                await self.delivery_gateway.send_private_text(claimed.user_id, text)
                await self.repository.advance_daily_report_delivery_chunk(claimed.user_id, claimed.report_date, index + 1)
        except PrivateDeliveryError as exc:
            await self.repository.mark_daily_report_delivery_terminal(claimed.user_id, claimed.report_date, str(exc))
        except Exception as exc:
            await self._retry(claimed, now, exc)
        else:
            await self.repository.mark_daily_report_delivery_sent(claimed.user_id, claimed.report_date, now.timestamp())

    async def _retry(self, delivery, now: datetime, error: Exception) -> None:
        delay = DAILY_REPORT_RETRY_SECONDS[min(delivery.attempts - 1, len(DAILY_REPORT_RETRY_SECONDS) - 1)]
        await self.repository.record_daily_report_delivery_failure(
            delivery.user_id, delivery.report_date, str(error), now.timestamp() + delay
        )

    async def _payload_for_delivery(self, delivery, now: datetime) -> dict | None:
        if delivery.payload_json is not None:
            payload = json.loads(delivery.payload_json)
            for chat_id in payload["chat_ids"]:
                try:
                    await self.permissions.assert_user_can_manage(delivery.user_id, int(chat_id))
                except PermissionUnavailable as exc:
                    await self._retry(delivery, now, exc)
                    return None
                except PermissionDenied:
                    await self.repository.mark_daily_report_delivery_sent(delivery.user_id, delivery.report_date, now.timestamp())
                    return None
            return payload
        cutoff = datetime.combine(datetime.fromisoformat(delivery.report_date).date(), self.push_time, tzinfo=self.timezone)
        chat_ids = await self.repository.list_user_stats_subscription_ids(delivery.user_id, self._utc_db_timestamp(cutoff))
        reports = []
        report_chat_ids = []
        for chat_id in chat_ids:
            try:
                await self.permissions.assert_user_can_manage(delivery.user_id, chat_id)
                reports.append(format_chat_report(await self.analytics.get_chat_report(delivery.user_id, chat_id, now), timezone=self.timezone))
                report_chat_ids.append(chat_id)
            except PermissionUnavailable as exc:
                await self._retry(delivery, now, exc)
                return None
            except PermissionDenied:
                continue
        if not reports:
            await self.repository.mark_daily_report_delivery_sent(delivery.user_id, delivery.report_date, now.timestamp())
            return None
        payload = {
            "chat_ids": report_chat_ids,
            "chunks": split_chat_reports(reports, header=f"📈 每日成员统计（{delivery.report_date}）"),
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if not await self.repository.store_daily_report_payload(delivery.user_id, delivery.report_date, serialized):
            await self._retry(delivery, now, RuntimeError("could not persist daily report payload"))
            return None
        return payload

    async def run_forever(self) -> None:
        self._stopping = False
        # Let polling settle before the first catch-up pass so startup cannot stampede private chats.
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=60)
        except TimeoutError:
            pass
        if self._stopping:
            return
        await self.repository.recover_stuck_daily_report_deliveries(self.clock())
        while not self._stopping:
            await self.run_due_once()
            if self._stopping:
                break
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=60)
            except TimeoutError:
                pass

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
