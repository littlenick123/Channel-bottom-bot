from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .domain import DailyMemberStats, MemberStatsReport
from .repositories import AuthorizationError, Repository


RUNTIME_GAP_SECONDS = 300
_ACTIVE_MEMBER_STATUSES = frozenset({"member", "administrator", "creator", "owner"})


class AnalyticsGateway(Protocol):
    async def get_member_count(self, chat_id: int) -> int: ...


def _value_name(value: object) -> str:
    value = getattr(value, "value", value)
    return str(value).lower().rsplit(".", 1)[-1]


def is_active_member(member: object) -> bool:
    """Return whether Telegram considers this member currently in the chat."""
    status = _value_name(getattr(member, "status", member))
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return status in _ACTIVE_MEMBER_STATUSES


def classify_member_transition(old_member: object, new_member: object) -> str | None:
    old_active = is_active_member(old_member)
    new_active = is_active_member(new_member)
    if old_active == new_active:
        return None
    return "join" if new_active else "leave"


class AnalyticsService:
    def __init__(self, repository: Repository, gateway: AnalyticsGateway, timezone: ZoneInfo | str = "Asia/Shanghai") -> None:
        self.repository = repository
        self.gateway = gateway
        self.timezone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone

    def local_date(self, instant: datetime) -> date:
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=ZoneInfo("UTC"))
        return instant.astimezone(self.timezone).date()

    @staticmethod
    def _timestamp(instant: datetime) -> float:
        if instant.tzinfo is None:
            return instant.replace(tzinfo=ZoneInfo("UTC")).timestamp()
        return instant.timestamp()

    async def record_member_update(self, update_id: int, event: Any) -> bool:
        direction = classify_member_transition(event.old_chat_member, event.new_chat_member)
        if direction is None:
            return False
        chat_id = int(event.chat.id)
        event_at = self._timestamp(event.date)
        return await self.repository.record_member_transition(
            int(update_id), chat_id, direction, event_at, self.local_date(event.date).isoformat()
        )

    async def initialize_channel(self, channel_id: int, activated_at: datetime) -> bool:
        return await self.repository.initialize_analytics(
            channel_id, self._timestamp(activated_at), self.local_date(activated_at).isoformat()
        )

    def _intersected_dates(self, started_at: datetime, ended_at: datetime) -> tuple[str, ...]:
        start = self.local_date(started_at)
        end = self.local_date(ended_at)
        if end < start:
            start, end = end, start
        dates: list[str] = []
        while start <= end:
            dates.append(start.isoformat())
            start += timedelta(days=1)
        return tuple(dates)

    async def mark_permission_gap(
        self, channel_id: int, started_at: datetime, ended_at: datetime, reason: str = "permission unavailable"
    ) -> None:
        await self.repository.mark_member_dates_incomplete(
            channel_id, self._intersected_dates(started_at, ended_at), reason
        )

    async def mark_runtime_gap(
        self, started_at: datetime, ended_at: datetime, reason: str = "runtime gap"
    ) -> bool:
        if self._timestamp(ended_at) - self._timestamp(started_at) <= RUNTIME_GAP_SECONDS:
            return False
        dates = self._intersected_dates(started_at, ended_at)
        for channel_id in await self.repository.list_stats_managed_channel_ids():
            state = await self.repository.get_analytics_state(channel_id)
            if state is None:
                continue
            activated_at = datetime.fromtimestamp(float(state["started_at"]), self.timezone)
            effective_start = max(started_at.astimezone(self.timezone), activated_at)
            if effective_start <= ended_at.astimezone(self.timezone):
                await self.repository.mark_member_dates_incomplete(
                    channel_id, self._intersected_dates(effective_start, ended_at), reason
                )
        return True

    async def heartbeat(self, now: datetime) -> bool:
        previous = await self.repository.get_analytics_heartbeat()
        now_timestamp = self._timestamp(now)
        marked_gap = False
        if previous is not None and now_timestamp - previous > RUNTIME_GAP_SECONDS:
            marked_gap = await self.mark_runtime_gap(
                datetime.fromtimestamp(previous, self.timezone), now, "runtime heartbeat gap"
            )
        await self.repository.set_analytics_heartbeat(now_timestamp)
        return marked_gap

    async def cleanup_processed_updates(self, now: datetime) -> int:
        return await self.repository.cleanup_processed_member_updates(self._timestamp(now) - timedelta(days=30).total_seconds())

    async def refresh_current_count(self, chat_id: int, now: datetime) -> int | None:
        try:
            count = await self.gateway.get_member_count(chat_id)
        except Exception:
            return None
        await self.repository.initialize_analytics(chat_id, self._timestamp(now), self.local_date(now).isoformat())
        await self.repository.set_member_count_cache(chat_id, int(count), self._timestamp(now))
        return int(count)

    def _report_day(self, row: DailyMemberStats | None, state, report_date: date) -> DailyMemberStats:
        if row is not None:
            return row
        if state is not None:
            started_at = datetime.fromtimestamp(float(state["started_at"]), self.timezone)
            local_start = datetime.combine(report_date, time.min, tzinfo=self.timezone)
            if started_at <= local_start:
                return DailyMemberStats(report_date)
        return DailyMemberStats(report_date, is_complete=False, incomplete_reason="statistics unavailable for this date")

    async def get_chat_report(self, user_id: int, chat_id: int, now: datetime) -> MemberStatsReport:
        subscribed = await self.repository.get_manager_stats_push_enabled(user_id, chat_id)
        if subscribed is None:
            raise AuthorizationError("user has not bound this channel")
        await self.refresh_current_count(chat_id, now)
        channel = await self.repository.get_channel(chat_id)
        if channel is None:
            raise AuthorizationError("channel is unavailable")
        current_day = self.local_date(now)
        previous_day = current_day - timedelta(days=1)
        state = await self.repository.get_analytics_state(chat_id)
        today = self._report_day(await self.repository.get_daily_member_stats(chat_id, current_day), state, current_day)
        yesterday = self._report_day(await self.repository.get_daily_member_stats(chat_id, previous_day), state, previous_day)
        return MemberStatsReport(
            chat_id=chat_id,
            chat_title=str(channel["title"]),
            chat_type=str(channel["chat_type"]),
            current_member_count=None if state is None or state["last_member_count"] is None else int(state["last_member_count"]),
            current_count_at=None if state is None or state["last_count_at"] is None else float(state["last_count_at"]),
            today=today,
            yesterday=yesterday,
            stats_push_enabled=subscribed,
        )


class MemberUpdateAdapter:
    """Bridges aiogram's update envelope to the analytics service."""

    def __init__(self, analytics: AnalyticsService) -> None:
        self.analytics = analytics

    async def handle(self, event, event_update) -> bool:
        return await self.analytics.record_member_update(int(event_update.update_id), event)
