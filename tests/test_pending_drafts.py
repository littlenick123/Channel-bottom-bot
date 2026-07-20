import tempfile
import unittest
from pathlib import Path

from bottom_post_bot.database import Database
from bottom_post_bot.drafts import IncomingContent
from bottom_post_bot.pending_drafts import PendingDraftService
from bottom_post_bot.repositories import Repository


class FakeStorage:
    def __init__(self) -> None:
        self.copy_calls: list[tuple[IncomingContent, ...]] = []
        self.deleted_ids: list[list[int]] = []
        self.delete_error: Exception | None = None

    async def copy_messages(self, messages):
        self.copy_calls.append(tuple(messages))
        return [700 + index for index, _ in enumerate(messages)]

    async def delete_storage_messages(self, message_ids):
        self.deleted_ids.append(list(message_ids))
        if self.delete_error:
            raise self.delete_error


class PendingDraftServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repo = Repository(self.db)
        await self.repo.upsert_user(1, "Alice")
        self.storage = FakeStorage()
        self.service = PendingDraftService(self.repo, self.storage, max_drafts=50, ttl_seconds=60, clock=lambda: 100.0)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_prepare_copies_once_and_confirm_uses_content_summary(self) -> None:
        pending = await self.service.prepare(
            1,
            [
                IncomingContent(10, 1, "  album\n title  ", "photo", "group", "[]"),
                IncomingContent(10, 2, None, "photo", "group", "[]"),
            ],
        )

        draft = await self.service.confirm(pending.id, 1)

        self.assertEqual(len(self.storage.copy_calls), 1)
        self.assertEqual(draft.name, "album title")
        self.assertEqual([item.storage_message_id for item in draft.current_revision.items], [700, 701])

    async def test_successful_discard_deletes_storage_items_and_completes_cleanup_row(self) -> None:
        pending = await self.service.prepare(1, [IncomingContent(10, 1, "discard", None, None, "[]")])

        discarded = await self.service.discard(pending.id, 1)

        self.assertTrue(discarded)
        self.assertEqual(self.storage.deleted_ids, [[700]])
        self.assertIsNone(await self.repo.get_pending_draft(1, pending.id))
        self.assertEqual(await self.repo.list_pending_cleanup(now=100.0), [])

    async def test_failed_discard_deletion_keeps_terminal_row_retryable(self) -> None:
        pending = await self.service.prepare(1, [IncomingContent(10, 1, "discard", None, None, "[]")])
        self.storage.delete_error = RuntimeError("temporary deletion failure")

        with self.assertRaisesRegex(RuntimeError, "temporary deletion failure"):
            await self.service.discard(pending.id, 1)

        cleanup = await self.repo.list_pending_cleanup(now=100.0)
        self.assertEqual([(item.id, item.status) for item in cleanup], [(pending.id, "discarded")])
        self.storage.delete_error = None
        self.assertEqual(await self.service.cleanup_expired(now=100.0), 1)
        self.assertIsNone(await self.repo.get_pending_draft(1, pending.id))

    async def test_discard_rejects_expired_pending_without_deleting_storage_items(self) -> None:
        pending = await self.service.prepare(1, [IncomingContent(10, 1, "expired", None, None, "[]")])
        self.service.clock = lambda: 160.0

        discarded = await self.service.discard(pending.id, 1)

        self.assertFalse(discarded)
        self.assertEqual(self.storage.deleted_ids, [])
        row = await self.db.fetch_one("SELECT status FROM pending_drafts WHERE id=?", (pending.id,))
        self.assertEqual(row["status"], "pending")

    async def test_expired_cleanup_retries_terminal_rows(self) -> None:
        pending = await self.service.prepare(1, [IncomingContent(10, 1, "expired", None, None, "[]")])

        cleaned = await self.service.cleanup_expired(now=160.0)

        self.assertEqual(cleaned, 1)
        self.assertEqual(self.storage.deleted_ids, [[700]])
        self.assertIsNone(await self.repo.get_pending_draft(1, pending.id))

    async def test_cleanup_keeps_terminal_row_when_deletion_fails_for_retry(self) -> None:
        pending = await self.service.prepare(1, [IncomingContent(10, 1, "retry", None, None, "[]")])
        self.storage.delete_error = RuntimeError("temporary deletion failure")

        cleaned = await self.service.cleanup_expired(now=160.0)

        self.assertEqual(cleaned, 0)
        row = await self.db.fetch_one("SELECT status FROM pending_drafts WHERE id=?", (pending.id,))
        self.assertEqual(row["status"], "expired")
        self.storage.delete_error = None
        self.assertEqual(await self.service.cleanup_expired(now=161.0), 1)
        self.assertEqual(self.storage.deleted_ids, [[700], [700]])

    async def test_pending_draft_can_be_confirmed_after_database_reopen_before_expiry(self) -> None:
        database_path = Path(self.tempdir.name) / "restart.sqlite3"
        first_database = await Database.open(database_path)
        first_repository = Repository(first_database)
        await first_repository.upsert_user(1, "Alice")
        first_service = PendingDraftService(
            first_repository,
            FakeStorage(),
            max_drafts=50,
            ttl_seconds=60,
            clock=lambda: 100.0,
        )
        pending = await first_service.prepare(1, [IncomingContent(10, 1, "restart", None, None, "[]")])
        await first_database.close()

        second_database = await Database.open(database_path)
        try:
            second_service = PendingDraftService(
                Repository(second_database),
                FakeStorage(),
                max_drafts=50,
                ttl_seconds=60,
                clock=lambda: 159.0,
            )

            draft = await second_service.confirm(pending.id, 1)
        finally:
            await second_database.close()

        self.assertEqual(draft.name, "restart")


if __name__ == "__main__":
    unittest.main()
