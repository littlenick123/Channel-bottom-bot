import tempfile
import unittest
from pathlib import Path

from bottom_post_bot.database import Database
from bottom_post_bot.domain import ContentItem
from bottom_post_bot.drafts import DraftService, IncomingContent, default_draft_name
from bottom_post_bot.repositories import Repository


class FakeStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[IncomingContent, ...]] = []

    async def copy_messages(self, messages):
        self.calls.append(tuple(messages))
        return [700 + index for index, _ in enumerate(messages)]


class DraftServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repo = Repository(self.db)
        await self.repo.upsert_user(1, "Alice")
        self.storage = FakeStorage()
        self.service = DraftService(self.repo, self.storage, max_drafts=50)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_capture_album_copies_once_and_preserves_order(self) -> None:
        messages = [
            IncomingContent(10, 1, "caption", "photo", "album-1", "[]"),
            IncomingContent(10, 2, None, "photo", "album-1", "[]"),
        ]

        draft = await self.service.capture(1, messages, name="Album")

        self.assertEqual(len(self.storage.calls), 1)
        self.assertEqual([item.storage_message_id for item in draft.current_revision.items], [700, 701])
        self.assertEqual([item.grouped_id for item in draft.current_revision.items], ["album-1", "album-1"])

    async def test_capture_text_creates_private_draft(self) -> None:
        draft = await self.service.capture(1, [IncomingContent(10, 1, "hello", None, None, "[]")])
        self.assertEqual(draft.owner_user_id, 1)
        self.assertEqual(draft.name, "hello")
        self.assertIsNone(await self.repo.get_draft(2, draft.id))

    async def test_capture_uses_normalized_default_name(self) -> None:
        draft = await self.service.capture(1, [IncomingContent(10, 1, "  hello\n  world ", None, None, "[]")])

        self.assertEqual(draft.name, "hello world")

    def test_default_draft_name_uses_first_non_blank_text_or_media_fallback(self) -> None:
        self.assertEqual(
            default_draft_name((ContentItem(text="  first\n title  "), ContentItem(text="second"))),
            "first title",
        )
        self.assertEqual(default_draft_name((ContentItem(storage_message_id=700, media_kind="photo"),)), "媒体草稿")

    async def test_copy_draft_creates_independent_personal_draft(self) -> None:
        draft = await self.service.capture(1, [IncomingContent(10, 1, "hello", None, None, "[]")])
        copied = await self.service.copy(draft.id, 1, "copy")
        self.assertNotEqual(copied.id, draft.id)
        self.assertEqual(copied.current_revision.items, draft.current_revision.items)


if __name__ == "__main__":
    unittest.main()
