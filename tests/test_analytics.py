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
        self.assertIsNone(classify_member_transition(member("left"), member("kicked")))
        self.assertIsNone(classify_member_transition(member("restricted", is_member=False), member("left")))
        self.assertIsNone(classify_member_transition(member("restricted", is_member=True), member("restricted", is_member=True)))

    async def test_records_once_at_timezone_midnight_and_ignores_unmanaged(self) -> None:
        joined = event(10, datetime(2026, 1, 1, 16, 1, tzinfo=UTC), member("left"), member("member"))
        self.assertTrue(await self.service.record_member_update(10, joined))
        self.assertFalse(await self.service.record_member_update(10, joined))
        stats = await self.repo.get_daily_member_stats(-1001, "2026-01-02")
        self.assertEqual((stats.joined_count, stats.left_count), (1, 0))
        self.assertFalse(await self.service.record_member_update(11, event(11, joined.date, member("left"), member("member"), channel_id=-1002)))

    async def test_backlogged_pre_activation_event_is_deduplicated_without_changing_counts(self) -> None:
        activated = datetime(2026, 1, 2, 0, tzinfo=UTC)
        await self.service.initialize_channel(-1001, activated)
        stale = event(12, activated - timedelta(minutes=1), member("left"), member("member"))

        self.assertFalse(await self.service.record_member_update(12, stale))
        self.assertFalse(await self.service.record_member_update(12, stale))

        row = await self.db.fetch_one("SELECT direction, ignored FROM processed_member_updates WHERE update_id=12")
        self.assertEqual((row["direction"], row["ignored"]), ("join", 1))
        stats = await self.repo.get_daily_member_stats(-1001, "2026-01-02")
        self.assertEqual((stats.joined_count, stats.left_count), (0, 0))

    async def test_interruption_marks_every_covered_local_day_when_access_returns(self) -> None:
        started = datetime(2026, 1, 1, 15, tzinfo=UTC)
        recovered = datetime(2026, 1, 3, 16, tzinfo=UTC)
        await self.service.initialize_channel(-1001, started)
        await self.service.begin_permission_interruption(-1001, started, "bot access lost")
        await self.service.end_permission_interruption(-1001, recovered)

        rows = [await self.repo.get_daily_member_stats(-1001, day) for day in ("2026-01-01", "2026-01-02", "2026-01-03")]
        self.assertTrue(all(row is not None and not row.is_complete for row in rows))
        state = await self.repo.get_analytics_state(-1001)
        self.assertIsNone(state["interruption_started_at"])

    async def test_activation_and_runtime_gaps_mark_every_intersected_date_incomplete(self) -> None:
        start = datetime(2026, 1, 1, 15, tzinfo=UTC)
        await self.service.initialize_channel(-1001, start)
        await self.service.mark_runtime_gap(start, start + timedelta(days=2, seconds=301), "worker unavailable")
        rows = [await self.repo.get_daily_member_stats(-1001, day) for day in ("2026-01-01", "2026-01-02", "2026-01-03")]
        self.assertTrue(all(row and not row.is_complete for row in rows))
        self.assertEqual(rows[0].incomplete_reason, "statistics started during this day")

    async def test_runtime_gap_does_not_create_history_for_uninitialized_chats_and_clamps_to_activation(self) -> None:
        await self.repo.upsert_channel(-1002, "Later", "later")
        await self.repo.bind_manager(7, -1002, 10)
        activation = datetime(2026, 1, 2, 15, tzinfo=UTC)
        await self.service.initialize_channel(-1001, activation)
        self.assertTrue(
            await self.service.mark_runtime_gap(
                datetime(2026, 1, 1, 15, tzinfo=UTC), datetime(2026, 1, 3, 15, 1, tzinfo=UTC)
            )
        )
        self.assertIsNone(await self.repo.get_daily_member_stats(-1001, "2026-01-01"))
        self.assertIsNone(await self.repo.get_daily_member_stats(-1002, "2026-01-02"))
        self.assertFalse((await self.repo.get_daily_member_stats(-1001, "2026-01-02")).is_complete)

    async def test_permission_gap_and_heartbeat_only_mark_after_threshold(self) -> None:
        start = datetime(2026, 1, 1, 15, tzinfo=UTC)
        await self.service.initialize_channel(-1001, start)
        await self.service.mark_permission_gap(-1001, start, start + timedelta(days=1), "permission lost")
        self.assertFalse((await self.repo.get_daily_member_stats(-1001, "2026-01-02")).is_complete)
        self.assertFalse(await self.service.heartbeat(start))
        self.assertFalse(await self.service.heartbeat(start + timedelta(seconds=300)))
        self.assertTrue(await self.service.heartbeat(start + timedelta(seconds=601)))

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
        await self.service.record_member_update(
            30, event(30, datetime(2026, 1, 2, tzinfo=UTC), member("left"), member("member"))
        )
        await self.repo.delete_channel_config(-1001, 7)
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM chat_analytics_state WHERE channel_id=-1001"))
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM member_daily_stats WHERE channel_id=-1001"))
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM processed_member_updates WHERE channel_id=-1001"))

    async def test_delivery_ledger_reserves_claims_retries_and_marks_sent(self) -> None:
        self.assertTrue(await self.repo.reserve_daily_report_delivery(7, "2026-01-02", 100.0))
        self.assertFalse(await self.repo.reserve_daily_report_delivery(7, "2026-01-02", 100.0))
        due = await self.repo.list_due_daily_report_deliveries(100.0)
        self.assertEqual([(delivery.user_id, delivery.report_date, delivery.attempts) for delivery in due], [(7, "2026-01-02", 0)])
        claimed = await self.repo.claim_daily_report_delivery(7, "2026-01-02", 100.0)
        self.assertEqual(claimed.attempts, 1)
        await self.repo.record_daily_report_delivery_failure(7, "2026-01-02", "offline", 200.0)
        self.assertEqual(await self.repo.list_due_daily_report_deliveries(199.0), [])
        self.assertEqual((await self.repo.claim_daily_report_delivery(7, "2026-01-02", 200.0)).attempts, 2)
        await self.repo.mark_daily_report_delivery_sent(7, "2026-01-02", 201.0)
        self.assertEqual(await self.repo.list_due_daily_report_deliveries(999.0), [])

    async def test_health_counts_include_analytics_integrity_delivery_failures_and_heartbeat(self) -> None:
        await self.service.initialize_channel(-1001, datetime(2026, 1, 1, 15, tzinfo=UTC))
        await self.repo.set_analytics_heartbeat(123.0)
        await self.repo.reserve_daily_report_delivery(7, "2026-01-02", 0.0)
        await self.repo.claim_daily_report_delivery(7, "2026-01-02", 0.0)
        await self.repo.record_daily_report_delivery_failure(7, "2026-01-02", "offline", 0.0)

        counts = await self.repo.health_counts()

        self.assertEqual(counts["analytics_incomplete_days"], 1)
        self.assertEqual(counts["daily_report_deliveries_failed"], 1)
        self.assertEqual(counts["daily_report_deliveries_due"], 1)
        self.assertEqual(counts["analytics_last_heartbeat_at"], 123.0)

    async def test_stats_subscriptions_are_independent_and_respect_report_cutoff(self) -> None:
        await self.repo.upsert_user(8, "Bob")
        await self.repo.bind_manager(8, -1001, 10)
        await self.db.execute("UPDATE channel_managers SET bound_at='2026-01-01 00:00:00' WHERE channel_id=-1001")
        await self.repo.upsert_channel(-1002, "Late", "late")
        await self.repo.bind_manager(8, -1002, 10)
        await self.db.execute(
            "UPDATE channel_managers SET bound_at='2026-01-02 00:06:00' WHERE user_id=8 AND channel_id=-1002"
        )
        await self.repo.set_manager_stats_push_enabled(7, -1001, False)

        self.assertEqual(await self.repo.list_daily_report_manager_ids("2026-01-02 00:05:00"), [8])
        self.assertEqual(await self.repo.list_user_stats_subscription_ids(8, "2026-01-02 00:05:00"), [-1001])
        self.assertEqual(await self.repo.list_user_stats_subscription_ids(7, "2026-01-02 00:05:00"), [])

    async def test_legacy_channel_upsert_preserves_stored_type_but_explicit_type_updates_it(self) -> None:
        await self.repo.upsert_channel(-1001, "News", "news", chat_type="supergroup")
        await self.repo.upsert_channel(-1001, "Renamed", "news")
        self.assertEqual((await self.repo.get_channel(-1001))["chat_type"], "supergroup")
        await self.repo.upsert_channel(-1001, "Renamed", "news", chat_type="channel")
        self.assertEqual((await self.repo.get_channel(-1001))["chat_type"], "channel")
