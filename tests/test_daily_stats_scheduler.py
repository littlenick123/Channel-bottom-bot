from datetime import UTC, datetime, time
from pathlib import Path
import tempfile
import unittest

from bottom_post_bot.analytics import AnalyticsService
from bottom_post_bot.database import Database
from bottom_post_bot.scheduler import DailyStatsScheduler, PrivateDeliveryError
from bottom_post_bot.repositories import Repository


class Gateway:
    async def get_member_count(self, chat_id: int) -> int:
        return abs(chat_id)


class Permissions:
    def __init__(self, denied: set[tuple[int, int]] | None = None) -> None:
        self.denied = denied or set()
        self.calls: list[tuple[int, int]] = []

    async def assert_user_can_manage(self, user_id: int, chat_id: int) -> None:
        self.calls.append((user_id, chat_id))
        if (user_id, chat_id) in self.denied:
            from bottom_post_bot.permissions import PermissionDenied
            raise PermissionDenied("not an admin")


class Delivery:
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = list(errors or [])
        self.calls: list[tuple[int, str]] = []

    async def send_private_text(self, user_id: int, text: str) -> None:
        self.calls.append((user_id, text))
        if self.errors:
            raise self.errors.pop(0)


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

    def test_due_at_uses_configured_local_timezone_and_time(self) -> None:
        self.assertEqual(
            self.scheduler.report_cutoff(datetime(2026, 1, 1, 16, 6, tzinfo=UTC)),
            datetime(2026, 1, 2, 0, 5, tzinfo=self.scheduler.timezone),
        )
