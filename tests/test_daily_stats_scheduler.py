from datetime import UTC, datetime, time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bottom_post_bot.analytics import AnalyticsService
from bottom_post_bot.database import Database
from bottom_post_bot.scheduler import DAILY_REPORT_RETRY_SECONDS, DailyStatsScheduler, PrivateDeliveryError
from bottom_post_bot.permissions import PermissionDenied, PermissionUnavailable
from bottom_post_bot.repositories import Repository


class Gateway:
    async def get_member_count(self, chat_id: int) -> int:
        return abs(chat_id)


class Permissions:
    def __init__(self, denied: set[tuple[int, int]] | None = None, unavailable: set[tuple[int, int]] | None = None) -> None:
        self.denied = denied or set()
        self.unavailable = unavailable or set()
        self.calls: list[tuple[int, int]] = []

    async def assert_user_can_manage(self, user_id: int, chat_id: int) -> None:
        self.calls.append((user_id, chat_id))
        if (user_id, chat_id) in self.unavailable:
            raise PermissionUnavailable("temporary")
        if (user_id, chat_id) in self.denied:
            raise PermissionDenied("not an admin")


class Delivery:
    def __init__(self, errors: list[Exception | None] | None = None) -> None:
        self.errors = list(errors or [])
        self.calls: list[tuple[int, str]] = []

    async def send_private_text(self, user_id: int, text: str) -> None:
        self.calls.append((user_id, text))
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error


class DailyStatsSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repo = Repository(self.db)
        for user_id, name in ((7, "Alice"), (8, "Bob")):
            await self.repo.upsert_user(user_id, name)
        for chat_id, title in ((-1001, "One"), (-1002, "Two")):
            await self.repo.upsert_channel(chat_id, title, None)
        await self.repo.bind_manager(7, -1001, 10)
        await self.repo.bind_manager(7, -1002, 10)
        await self.repo.bind_manager(8, -1002, 10)
        await self.db.execute("UPDATE channel_managers SET bound_at='2026-01-01 00:00:00'")
        self.analytics = AnalyticsService(self.repo, Gateway(), "Asia/Shanghai")
        self.delivery = Delivery()
        self.permissions = Permissions()
        self.scheduler = DailyStatsScheduler(
            self.repo, self.analytics, self.permissions, self.delivery,
            timezone="Asia/Shanghai", push_time=time(0, 5), clock=lambda: datetime(2026, 1, 2, 0, 6, tzinfo=UTC).timestamp(),
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_due_delivery_consolidates_subscribed_chats_and_suppresses_duplicates(self) -> None:
        now = datetime(2026, 1, 2, 0, 6, tzinfo=UTC)

        await self.scheduler.run_due_once(now)
        await self.scheduler.run_due_once(now)

        self.assertEqual([user_id for user_id, _ in self.delivery.calls], [7, 8])
        self.assertIn("One", self.delivery.calls[0][1])
        self.assertIn("Two", self.delivery.calls[0][1])
        self.assertIn("Two", self.delivery.calls[1][1])

    async def test_transient_retry_uses_required_backoff_and_blocked_user_does_not_stop_others(self) -> None:
        self.delivery.errors = [RuntimeError("network"), PrivateDeliveryError("blocked"), RuntimeError("network")]
        now = datetime(2026, 1, 2, 0, 6, tzinfo=UTC)

        await self.scheduler.run_due_once(now)

        alice = await self.db.fetch_one("SELECT status, attempts, next_attempt_at FROM daily_report_deliveries WHERE user_id=7")
        bob = await self.db.fetch_one("SELECT status FROM daily_report_deliveries WHERE user_id=8")
        self.assertEqual((alice["status"], alice["attempts"], alice["next_attempt_at"]), ("retry", 1, now.timestamp() + 60))
        self.assertEqual(bob["status"], "terminal")

    async def test_transient_permission_check_retries_without_silently_omitting_the_chat(self) -> None:
        self.permissions.unavailable.add((7, -1001))
        await self.repo.set_manager_stats_push_enabled(7, -1002, False)
        await self.repo.set_manager_stats_push_enabled(8, -1002, False)
        now = datetime(2026, 1, 2, 0, 6, tzinfo=UTC)

        await self.scheduler.run_due_once(now)

        row = await self.db.fetch_one("SELECT status, next_attempt_at FROM daily_report_deliveries WHERE user_id=7")
        self.assertEqual((row["status"], row["next_attempt_at"]), ("retry", now.timestamp() + 60))
        self.assertEqual(self.delivery.calls, [])

    async def test_retries_each_required_backoff_delay(self) -> None:
        self.delivery.errors = [RuntimeError("network")] * 5
        await self.repo.set_manager_stats_push_enabled(7, -1002, False)
        await self.repo.set_manager_stats_push_enabled(8, -1002, False)
        now = datetime(2026, 1, 2, 0, 6, tzinfo=UTC)
        for expected_delay in DAILY_REPORT_RETRY_SECONDS:
            await self.scheduler.run_due_once(now)
            row = await self.db.fetch_one("SELECT status, next_attempt_at FROM daily_report_deliveries WHERE user_id=7")
            self.assertEqual((row["status"], row["next_attempt_at"]), ("retry", now.timestamp() + expected_delay))
            now = datetime.fromtimestamp(now.timestamp() + expected_delay, UTC)

    async def test_binding_after_cutoff_is_excluded(self) -> None:
        await self.repo.upsert_channel(-1003, "Late", None)
        await self.repo.bind_manager(7, -1003, 10)
        await self.db.execute("UPDATE channel_managers SET bound_at='2026-01-01 16:06:00' WHERE user_id=7 AND channel_id=-1003")
        now = datetime(2026, 1, 2, 0, 6, tzinfo=UTC)

        await self.scheduler.run_due_once(now)

        self.assertNotIn("Late", self.delivery.calls[0][1])

    async def test_retry_resumes_at_failed_chunk_without_resending_prior_chunk(self) -> None:
        await self.repo.set_manager_stats_push_enabled(7, -1002, False)
        await self.repo.set_manager_stats_push_enabled(8, -1002, False)
        for number in range(15):
            chat_id = -1100 - number
            await self.repo.upsert_channel(chat_id, "x" * 200, None)
            await self.repo.bind_manager(7, chat_id, 30)
        await self.db.execute("UPDATE channel_managers SET bound_at='2026-01-01 00:00:00'")
        self.delivery.errors = [None, RuntimeError("network")]
        now = datetime(2026, 1, 2, 0, 6, tzinfo=UTC)

        await self.scheduler.run_due_once(now)

        row = await self.db.fetch_one("SELECT status, next_chunk_index, payload_json FROM daily_report_deliveries WHERE user_id=7")
        self.assertEqual((row["status"], row["next_chunk_index"]), ("retry", 1))
        self.assertIsNotNone(row["payload_json"])
        first_chunk, failed_chunk = (text for _, text in self.delivery.calls)

        await self.scheduler.run_due_once(datetime.fromtimestamp(now.timestamp() + 60, UTC))

        self.assertEqual(len(self.delivery.calls), 3)
        self.assertEqual(self.delivery.calls[2][1], failed_chunk)
        self.assertNotEqual(self.delivery.calls[2][1], first_chunk)

    async def test_definitive_permission_loss_excludes_chat_without_retry(self) -> None:
        self.permissions.denied.add((7, -1001))
        await self.repo.set_manager_stats_push_enabled(7, -1002, False)
        await self.repo.set_manager_stats_push_enabled(8, -1002, False)
        now = datetime(2026, 1, 2, 0, 6, tzinfo=UTC)

        await self.scheduler.run_due_once(now)

        row = await self.db.fetch_one("SELECT status FROM daily_report_deliveries WHERE user_id=7")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(self.delivery.calls, [])

    async def test_startup_waits_sixty_seconds_then_recovers_stuck_deliveries(self) -> None:
        self.repo.recover_stuck_daily_report_deliveries = AsyncMock(return_value=1)

        async def stop_after_first_run():
            self.scheduler.stop()

        self.scheduler.run_due_once = AsyncMock(side_effect=stop_after_first_run)
        async def immediately_wake(awaitable, *, timeout):
            awaitable.close()
            return None

        with patch("bottom_post_bot.scheduler.asyncio.wait_for", new=AsyncMock(side_effect=immediately_wake)) as wait_for:
            await self.scheduler.run_forever()

        wait_for.assert_awaited_once()
        self.assertEqual(wait_for.await_args.kwargs["timeout"], 60)
        self.repo.recover_stuck_daily_report_deliveries.assert_awaited_once()

    async def test_wake_and_stop_signal_scheduler_event(self) -> None:
        self.scheduler._wake.clear()
        self.scheduler.wake()
        self.assertTrue(self.scheduler._wake.is_set())
        self.scheduler._wake.clear()
        self.scheduler.stop()
        self.assertTrue(self.scheduler._stopping)
        self.assertTrue(self.scheduler._wake.is_set())

    async def test_due_run_updates_heartbeat_and_cleanup_before_cutoff(self) -> None:
        self.analytics.heartbeat = AsyncMock(return_value=False)
        self.analytics.cleanup_processed_updates = AsyncMock(return_value=0)
        now = datetime(2026, 1, 1, 16, 4, tzinfo=UTC)

        await self.scheduler.run_due_once(now)

        local_now = now.astimezone(self.scheduler.timezone)
        self.analytics.heartbeat.assert_awaited_once_with(local_now)
        self.analytics.cleanup_processed_updates.assert_awaited_once_with(local_now)

    async def test_stuck_sending_delivery_is_recovered_as_due(self) -> None:
        await self.repo.reserve_daily_report_delivery(7, "2026-01-02", 100.0)
        self.assertIsNotNone(await self.repo.claim_daily_report_delivery(7, "2026-01-02", 100.0))

        self.assertEqual(await self.repo.recover_stuck_daily_report_deliveries(200.0), 1)

        due = await self.repo.list_due_daily_report_deliveries(200.0)
        self.assertEqual([(item.user_id, item.report_date, item.status) for item in due], [(7, "2026-01-02", "retry")])

    def test_due_at_uses_configured_local_timezone_and_time(self) -> None:
        self.assertEqual(
            self.scheduler.report_cutoff(datetime(2026, 1, 1, 16, 6, tzinfo=UTC)),
            datetime(2026, 1, 2, 0, 5, tzinfo=self.scheduler.timezone),
        )
