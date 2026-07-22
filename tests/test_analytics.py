from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from bottom_post_bot.analytics import AnalyticsService, classify_member_transition, is_active_member
from bottom_post_bot.database import Database
from bottom_post_bot.repositories import Repository


def member(status: str, *, is_member: bool = False):
    return SimpleNamespace(status=status, is_member=is_member)


def event(update_id: int, when: datetime, old: object, new: object, *, channel_id: int = -1001):
    return SimpleNamespace(
        chat=SimpleNamespace(id=channel_id, title="News", type="channel"),
        old_chat_member=old,
        new_chat_member=new,
        date=when,
    )


class Gateway:
    def __init__(self, value: int | Exception = 42) -> None:
        self.value = value

    async def get_member_count(self, chat_id: int) -> int:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class AnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repo = Repository(self.db)
        await self.repo.upsert_user(7, "Alice")
        await self.repo.upsert_channel(-1001, "News", "news")
        await self.repo.bind_manager(7, -1001, 10)
        self.service = AnalyticsService(self.repo, Gateway(), "Asia/Shanghai")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    def test_classifies_active_and_restricted_transitions(self) -> None:
        self.assertTrue(is_active_member(member("member")))
        self.assertTrue(is_active_member(member("administrator")))
        self.assertTrue(is_active_member(member("creator")))
        self.assertTrue(is_active_member(member("restricted", is_member=True)))
        self.assertFalse(is_active_member(member("restricted", is_member=False)))
        self.assertFalse(is_active_member(member("left")))
        self.assertEqual(classify_member_transition(member("left"), member("restricted", is_member=True)), "join")
        self.assertEqual(classify_member_transition(member("restricted", is_member=True), member("kicked")), "leave")
        self.assertIsNone(classify_member_transition(member("member"), member("administrator")))

    async def test_records_once_at_timezone_midnight_and_ignores_unmanaged(self) -> None:
        joined = event(10, datetime(2026, 1, 1, 16, 1, tzinfo=UTC), member("left"), member("member"))
        self.assertTrue(await self.service.record_member_update(10, joined))
        self.assertFalse(await self.service.record_member_update(10, joined))
        stats = await self.repo.get_daily_member_stats(-1001, "2026-01-02")
        self.assertEqual((stats.joined_count, stats.left_count), (1, 0))
        self.assertFalse(await self.service.record_member_update(11, event(11, joined.date, member("left"), member("member"), channel_id=-1002)))

    async def test_activation_and_runtime_gaps_mark_every_intersected_date_incomplete(self) -> None:
        start = datetime(2026, 1, 1, 15, tzinfo=UTC)
        await self.service.initialize_channel(-1001, start)
        await self.service.mark_runtime_gap(start, start + timedelta(days=2, seconds=301), "worker unavailable")
        rows = [await self.repo.get_daily_member_stats(-1001, day) for day in ("2026-01-01", "2026-01-02", "2026-01-03")]
        self.assertTrue(all(row and not row.is_complete for row in rows))
        self.assertEqual(rows[0].incomplete_reason, "statistics started during this day")

    async def test_cleanup_refresh_and_report_cache_fallbacks(self) -> None:
        now = datetime(2026, 2, 1, 2, tzinfo=UTC)
        await self.service.record_member_update(20, event(20, now - timedelta(days=31), member("left"), member("member")))
        await self.service.record_member_update(21, event(21, now, member("member"), member("left")))
        self.assertEqual(await self.service.cleanup_processed_updates(now), 1)
        self.assertEqual(await self.service.refresh_current_count(-1001, now), 42)
        self.service.gateway.value = RuntimeError("offline")
        report = await self.service.get_chat_report(7, -1001, now)
        self.assertEqual((report.current_member_count, report.current_count_at), (42, now.timestamp()))
        self.assertEqual((report.today.left_count, report.today.net_change), (1, -1))
        await self.repo.clear_member_count_cache(-1001)
        report = await self.service.get_chat_report(7, -1001, now)
        self.assertIsNone(report.current_member_count)
        self.assertIsNone(report.current_count_at)

    async def test_stats_cascade_with_channel_deletion(self) -> None:
        await self.service.initialize_channel(-1001, datetime(2026, 1, 1, tzinfo=UTC))
        await self.repo.delete_channel_config(-1001, 7)
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM chat_analytics_state WHERE channel_id=-1001"))
