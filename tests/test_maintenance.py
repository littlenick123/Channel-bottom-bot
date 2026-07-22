import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bottom_post_bot import app
from bottom_post_bot.config import Settings
from bottom_post_bot.maintenance import PendingCleanupLoop


class FakePendingDraftService:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[float] = []
        self.first_call = asyncio.Event()
        self.second_call = asyncio.Event()

    async def cleanup_expired(self, now: float) -> int:
        self.calls.append(now)
        self.first_call.set()
        if len(self.calls) == 2:
            self.second_call.set()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary cleanup failure")
        return 0


class PendingCleanupLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_cleanup_immediately_and_stops(self) -> None:
        service = FakePendingDraftService()
        loop = PendingCleanupLoop(service, interval_seconds=60)
        task = asyncio.create_task(loop.run_forever())

        await asyncio.wait_for(service.first_call.wait(), timeout=0.5)
        loop.stop()
        await asyncio.wait_for(task, timeout=0.5)

        self.assertEqual(len(service.calls), 1)

    async def test_logs_cleanup_exception_and_continues(self) -> None:
        service = FakePendingDraftService(failures=1)
        loop = PendingCleanupLoop(service, interval_seconds=0.01)

        with self.assertLogs("bottom_post_bot.maintenance", level="ERROR"):
            task = asyncio.create_task(loop.run_forever())
            await asyncio.wait_for(service.second_call.wait(), timeout=0.5)
            loop.stop()
            await asyncio.wait_for(task, timeout=0.5)

        self.assertEqual(len(service.calls), 2)

    async def test_app_stops_pending_cleanup_before_closing_database(self) -> None:
        cleanup_started = asyncio.Event()
        cleanup_finished = asyncio.Event()
        cleanup_instances = []

        class FakeDatabase:
            closed_after_cleanup = False

            async def close(self) -> None:
                self.closed_after_cleanup = cleanup_finished.is_set()

        class FakeBot:
            def __init__(self, token: str) -> None:
                self.session = SimpleNamespace(close=AsyncMock())

            async def get_chat(self, chat_id: int) -> None:
                return None

            async def set_my_commands(self, commands) -> None:
                return None

            async def get_me(self):
                return SimpleNamespace(id=1)

        class FakeDispatcher:
            def include_router(self, router) -> None:
                return None

            def resolve_used_update_types(self) -> list[str]:
                return []

            async def start_polling(self, *args, **kwargs) -> None:
                await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)

        class FakeRepository:
            def __init__(self, database) -> None:
                return None

            async def recover_incomplete_batches(self, now: float) -> int:
                return 0

            async def list_managed_channels(self):
                return []

        class FakeScheduler:
            def __init__(self, *args, **kwargs) -> None:
                self._stop = asyncio.Event()

            async def run_forever(self) -> None:
                await self._stop.wait()

            def stop(self) -> None:
                self._stop.set()

        class FakeCleanupLoop:
            def __init__(self, service, interval_seconds: int) -> None:
                self.service = service
                self.interval_seconds = interval_seconds
                self._stop = asyncio.Event()
                cleanup_instances.append(self)

            async def run_forever(self) -> None:
                cleanup_started.set()
                try:
                    await self._stop.wait()
                finally:
                    cleanup_finished.set()

            def stop(self) -> None:
                self._stop.set()

        database = FakeDatabase()
        with tempfile.TemporaryDirectory() as tempdir:
            settings = Settings(
                bot_token="token",
                storage_channel_id=-1001,
                operator_user_ids=frozenset({1}),
                database_path=Path(tempdir) / "bot.sqlite3",
                pending_cleanup_interval_seconds=17,
            )
            with (
                patch.object(app.Database, "open", new=AsyncMock(return_value=database)),
                patch.object(app, "Bot", FakeBot),
                patch.object(app, "Dispatcher", FakeDispatcher),
                patch.object(app, "Repository", FakeRepository),
                patch.object(app, "RefreshScheduler", FakeScheduler),
                patch.object(app, "PendingCleanupLoop", FakeCleanupLoop, create=True),
            ):
                await app.run(settings)

        self.assertEqual(len(cleanup_instances), 1)
        self.assertEqual(cleanup_instances[0].interval_seconds, 17)
        self.assertTrue(database.closed_after_cleanup)

    async def test_app_drains_tracked_updates_added_during_shutdown_before_closing_resources(self) -> None:
        events: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        dispatcher_instances = []
        all_update_tasks: list[asyncio.Task] = []

        class FakeDatabase:
            async def close(self) -> None:
                events.append("database-closed")

        class FakeSession:
            async def close(self) -> None:
                events.append("session-closed")

        class FakeBot:
            def __init__(self, token: str) -> None:
                self.session = FakeSession()

            async def get_chat(self, chat_id: int) -> None:
                return None

            async def set_my_commands(self, commands) -> None:
                return None

            async def get_me(self):
                return SimpleNamespace(id=1)

        class FakeDispatcher:
            def __init__(self) -> None:
                self._handle_update_tasks: set[asyncio.Task] = set()
                dispatcher_instances.append(self)

            def include_router(self, router) -> None:
                return None

            def resolve_used_update_types(self) -> list[str]:
                return []

            def track(self, coroutine) -> None:
                task = asyncio.create_task(coroutine)
                all_update_tasks.append(task)
                self._handle_update_tasks.add(task)
                task.add_done_callback(self._handle_update_tasks.discard)

            async def start_polling(self, *args, **kwargs) -> None:
                async def second_handler() -> None:
                    second_started.set()
                    await release_second.wait()
                    events.append("second-update-finished")

                async def first_handler() -> None:
                    first_started.set()
                    await release_first.wait()
                    self.track(second_handler())
                    events.append("first-update-finished")

                self.track(first_handler())
                await first_started.wait()

        class FakeRepository:
            def __init__(self, database) -> None:
                return None

            async def recover_incomplete_batches(self, now: float) -> int:
                return 0

            async def list_managed_channels(self):
                return []

        class FakeLoop:
            def __init__(self, *args, **kwargs) -> None:
                self._stop = asyncio.Event()

            async def run_forever(self) -> None:
                await self._stop.wait()

            def stop(self) -> None:
                self._stop.set()

        class FakeHandlers:
            def __init__(self, *args, **kwargs) -> None:
                return None

            async def flush_albums(self) -> None:
                events.append("albums-flushed")

        database = FakeDatabase()
        with tempfile.TemporaryDirectory() as tempdir:
            settings = Settings(
                bot_token="token",
                storage_channel_id=-1001,
                operator_user_ids=frozenset({1}),
                database_path=Path(tempdir) / "bot.sqlite3",
            )
            with (
                patch.object(app.Database, "open", new=AsyncMock(return_value=database)),
                patch.object(app, "Bot", FakeBot),
                patch.object(app, "Dispatcher", FakeDispatcher),
                patch.object(app, "Repository", FakeRepository),
                patch.object(app, "RefreshScheduler", FakeLoop),
                patch.object(app, "PendingCleanupLoop", FakeLoop),
                patch.object(app, "BotHandlers", FakeHandlers),
                patch.object(app, "build_router", return_value=SimpleNamespace()),
            ):
                run_task = asyncio.create_task(app.run(settings))
                try:
                    await asyncio.wait_for(first_started.wait(), timeout=0.5)
                    await asyncio.sleep(0)
                    self.assertFalse(run_task.done())
                    self.assertNotIn("albums-flushed", events)
                    self.assertNotIn("session-closed", events)
                    self.assertNotIn("database-closed", events)

                    release_first.set()
                    await asyncio.wait_for(second_started.wait(), timeout=0.5)
                    await asyncio.sleep(0)
                    self.assertFalse(run_task.done())
                    self.assertNotIn("session-closed", events)
                    self.assertNotIn("database-closed", events)

                    release_second.set()
                    await asyncio.wait_for(run_task, timeout=0.5)
                finally:
                    release_first.set()
                    release_second.set()
                    await asyncio.gather(run_task, *all_update_tasks, return_exceptions=True)

        self.assertEqual(len(dispatcher_instances), 1)
        self.assertLess(events.index("second-update-finished"), events.index("albums-flushed"))
        self.assertLess(events.index("albums-flushed"), events.index("session-closed"))
        self.assertLess(events.index("session-closed"), events.index("database-closed"))

    async def test_update_task_drain_collects_and_logs_handler_exceptions(self) -> None:
        async def failing_handler() -> None:
            raise RuntimeError("handler failed")

        dispatcher = SimpleNamespace(_handle_update_tasks=set())
        task = asyncio.create_task(failing_handler())
        dispatcher._handle_update_tasks.add(task)
        task.add_done_callback(dispatcher._handle_update_tasks.discard)

        with self.assertLogs("bottom_post_bot.app", level="ERROR"):
            await app.drain_dispatcher_update_tasks(dispatcher)


if __name__ == "__main__":
    unittest.main()
