import tempfile
import unittest
from pathlib import Path

from bottom_post_bot.database import Database
from bottom_post_bot.publisher import FloodWaitSignal, RefreshOutcome
from bottom_post_bot.repositories import Repository
from bottom_post_bot.scheduler import RefreshScheduler


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class FakePublisher:
    def __init__(self, outcomes=None) -> None:
        self.outcomes = list(outcomes or [RefreshOutcome.SUCCESS])
        self.calls: list[int] = []

    async def refresh(self, channel_id: int):
        self.calls.append(channel_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeNotifier:
    def __init__(self) -> None:
        self.calls = []

    async def notify_channel_admins(self, channel_id: int, text: str) -> None:
        self.calls.append((channel_id, text))


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repo = Repository(self.db)
        await self.repo.upsert_channel(-1001, "Channel", None)
        self.clock = FakeClock()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_repeated_request_moves_deadline_and_increments_generation(self) -> None:
        scheduler = RefreshScheduler(self.repo, FakePublisher(), clock=self.clock.time)
        await scheduler.request(-1001, "first", 10)
        self.clock.now = 105
        await scheduler.request(-1001, "second", 10)

        job = await self.repo.get_refresh_job(-1001)
        self.assertEqual(job.due_at, 115)
        self.assertEqual(job.generation, 2)
        self.assertEqual(job.reason, "second")

    async def test_due_successful_job_is_removed(self) -> None:
        publisher = FakePublisher()
        scheduler = RefreshScheduler(self.repo, publisher, clock=self.clock.time)
        await scheduler.request(-1001, "new-message", 0)

        await scheduler.run_due_once()

        self.assertEqual(publisher.calls, [-1001])
        self.assertIsNone(await self.repo.get_refresh_job(-1001))

    async def test_regular_failure_uses_backoff(self) -> None:
        publisher = FakePublisher([RefreshOutcome.RETRY])
        scheduler = RefreshScheduler(self.repo, publisher, clock=self.clock.time)
        await scheduler.request(-1001, "new-message", 0)

        await scheduler.run_due_once()

        job = await self.repo.get_refresh_job(-1001)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.due_at, 105)

    async def test_flood_wait_uses_requested_delay_without_incrementing_attempts(self) -> None:
        publisher = FakePublisher([FloodWaitSignal(42)])
        scheduler = RefreshScheduler(self.repo, publisher, clock=self.clock.time)
        await scheduler.request(-1001, "new-message", 0)

        await scheduler.run_due_once()

        job = await self.repo.get_refresh_job(-1001)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.due_at, 142)

    async def test_fifth_regular_failure_pauses_and_notifies(self) -> None:
        await self.repo.schedule_refresh(-1001, 100, "retry")
        for _ in range(4):
            job = await self.repo.get_refresh_job(-1001)
            await self.repo.retry_refresh(-1001, job.generation, 100, "fail", increment_attempts=True)
        notifier = FakeNotifier()
        scheduler = RefreshScheduler(
            self.repo,
            FakePublisher([RefreshOutcome.RETRY]),
            clock=self.clock.time,
            notifier=notifier,
        )

        await scheduler.run_due_once()

        channel = await self.repo.get_channel(-1001)
        self.assertEqual(channel["status"], "paused")
        self.assertEqual(notifier.calls, [(-1001, "频道/超级群组连续发布失败五次，自动置底已暂停。")])

    async def test_permission_pause_notice_uses_channel_or_supergroup_wording(self) -> None:
        await self.repo.upsert_channel(-1001, "Group", None, chat_type="supergroup")
        notifier = FakeNotifier()
        scheduler = RefreshScheduler(
            self.repo,
            FakePublisher([RefreshOutcome.PAUSED]),
            clock=self.clock.time,
            notifier=notifier,
        )
        await scheduler.request(-1001, "new-message", 0)

        await scheduler.run_due_once()

        self.assertEqual(notifier.calls, [(-1001, "频道/超级群组发布权限或存储配置不可用，自动置底已暂停。")])


if __name__ == "__main__":
    unittest.main()
