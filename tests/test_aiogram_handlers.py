import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bottom_post_bot.domain import ContentItem, Draft, DraftRevision, PendingDraft, SlotSnapshot
from bottom_post_bot.handlers import BotHandlers, message_to_incoming
from bottom_post_bot.permissions import PermissionUnavailable
from bottom_post_bot.repositories import AuthorizationError, ResourceLimitError


class FakeRepository:
    def __init__(self) -> None:
        self.conversations = {}
        self.cleared = []
        self.set_states = []

    async def upsert_user(self, user_id, display_name):
        return None

    async def get_conversation(self, user_id):
        return self.conversations.get(user_id)

    async def clear_conversation(self, user_id):
        self.cleared.append(user_id)
        self.conversations.pop(user_id, None)

    async def set_conversation(self, user_id, state, payload, expires_at):
        self.conversations[user_id] = (state, payload)
        self.set_states.append((user_id, state, payload))

    async def get_draft(self, user_id, draft_id):
        return None


class FakeDraftService:
    def __init__(self) -> None:
        self.capture_calls = []
        self.update_buttons = AsyncMock()

    async def capture(self, *args):
        self.capture_calls.append(args)
        raise AssertionError("forwarded content must be prepared before creating a draft")


class FakePendingDrafts:
    def __init__(self) -> None:
        self.prepare_calls = []
        self.confirm_calls = []
        self.discard_calls = []
        self.fail_confirm_with = None
        self.fail_confirmable_with = None
        self.confirmable_calls = []
        self.draft = Draft(11, 42, "保存的草稿", DraftRevision(12, 1, (ContentItem(text="正文"),)))

    async def prepare(self, user_id, messages):
        self.prepare_calls.append((user_id, tuple(messages)))
        return PendingDraft(
            7,
            user_id,
            tuple(
                ContentItem(
                    text=item.text,
                    storage_message_id=700 + index,
                    media_kind=item.media_kind,
                    telegram_file_id=item.file_id,
                    grouped_id=item.grouped_id,
                    formatting_entities_json=item.formatting_entities_json,
                )
                for index, item in enumerate(messages)
            ),
            999.0,
        )

    async def confirm(self, pending_id, user_id, name=None):
        self.confirm_calls.append((pending_id, user_id, name))
        if self.fail_confirm_with:
            raise self.fail_confirm_with
        return self.draft

    async def assert_confirmable(self, pending_id, user_id):
        self.confirmable_calls.append((pending_id, user_id))
        if self.fail_confirmable_with:
            raise self.fail_confirmable_with

    async def discard(self, pending_id, user_id):
        self.discard_calls.append((pending_id, user_id))
        return True


class FakeGateway:
    def __init__(self) -> None:
        self.preview_calls = []
        self.preview_error = None

    async def preview_storage_messages(self, user_id, message_ids):
        self.preview_calls.append((user_id, list(message_ids)))
        if self.preview_error:
            raise self.preview_error
        return [900 + index for index, _ in enumerate(message_ids)]


class FakeMessage(SimpleNamespace):
    def __init__(self, **kwargs) -> None:
        self.answers = []
        super().__init__(**kwargs)

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


def forwarded_message(message_id=1, *, media_group_id=None, text="转发正文"):
    return FakeMessage(
        chat=SimpleNamespace(id=900, type="private"),
        message_id=message_id,
        text=text,
        caption=None,
        entities=[],
        caption_entities=None,
        media_group_id=media_group_id,
        photo=None,
        video=None,
        animation=None,
        document=None,
        audio=None,
        voice=None,
        sticker=None,
        forward_origin=SimpleNamespace(chat=SimpleNamespace(id=-1007)),
        forward_from_chat=None,
        from_user=SimpleNamespace(id=42, first_name="Alice", last_name=None, full_name="Alice"),
    )


class PendingDraftConfirmationHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.drafts = FakeDraftService()
        self.pending = FakePendingDrafts()
        self.gateway = FakeGateway()
        self.settings = SimpleNamespace(
            conversation_timeout_seconds=900,
            max_drafts_per_user=50,
            max_slots_per_channel=10,
            operator_user_ids=frozenset(),
        )
        self.handlers = BotHandlers(
            SimpleNamespace(),
            self.repository,
            self.drafts,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            self.gateway,
            self.settings,
            pending_drafts=self.pending,
        )

    async def test_forwarded_post_prepares_previews_and_does_not_create_formal_draft(self) -> None:
        message = forwarded_message()

        await self.handlers.on_private_message(message)

        self.assertEqual(len(self.pending.prepare_calls), 1)
        self.assertEqual(self.drafts.capture_calls, [])
        self.assertEqual(self.gateway.preview_calls, [(42, [700])])
        self.assertEqual(message.answers[-1][0], "已保存预览，请确认是否保存为草稿。")
        callbacks = [button.callback_data for row in message.answers[-1][1].inline_keyboard for button in row]
        self.assertEqual(callbacks, ["p:s:7", "p:n:7", "p:x:7"])

    async def test_preview_failure_still_sends_warning_and_usable_confirmation_keyboard(self) -> None:
        self.gateway.preview_error = RuntimeError("private chat unavailable")
        message = forwarded_message()

        await self.handlers.on_private_message(message)

        self.assertEqual(message.answers[0][0], "预览发送失败，但待确认内容已保存。")
        callbacks = [button.callback_data for row in message.answers[-1][1].inline_keyboard for button in row]
        self.assertEqual(callbacks, ["p:s:7", "p:n:7", "p:x:7"])

    async def test_album_prepares_once_in_message_id_order(self) -> None:
        first = forwarded_message(2, media_group_id="album")
        second = forwarded_message(1, media_group_id="album")
        key = (900, "album")
        self.handlers._album_messages[key] = [first, second]

        with patch("bottom_post_bot.handlers.asyncio.sleep", new=AsyncMock()):
            await self.handlers._flush_album_after(key)

        self.assertEqual(len(self.pending.prepare_calls), 1)
        self.assertEqual([item.source_message_id for item in self.pending.prepare_calls[0][1]], [1, 2])
        self.assertEqual(self.gateway.preview_calls, [(42, [700, 701])])

    async def test_callbacks_save_name_or_discard_pending_draft(self) -> None:
        event = SimpleNamespace()
        self.handlers._show_draft = AsyncMock()
        self.handlers._show = AsyncMock()
        self.handlers._show_drafts = AsyncMock()

        await self.handlers._dispatch_callback(event, 42, "p:s:7")
        await self.handlers._dispatch_callback(event, 42, "p:n:8")
        await self.handlers._dispatch_callback(event, 42, "p:x:9")

        self.assertEqual(self.pending.confirm_calls, [(7, 42, None)])
        self.handlers._show_draft.assert_awaited_once_with(event, 42, 11)
        self.assertEqual(self.pending.confirmable_calls, [(8, 42)])
        self.assertEqual(self.repository.set_states[-1][:3], (42, "await_pending_name", {"pending_id": 8}))
        self.assertEqual(self.pending.discard_calls, [(9, 42)])
        self.handlers._show_drafts.assert_awaited_once_with(event, 42)

    async def test_named_save_rejects_foreign_or_expired_pending_without_setting_state(self) -> None:
        event = SimpleNamespace()
        self.handlers._show = AsyncMock()
        for failure in (AuthorizationError("pending draft already processed or expired"), AuthorizationError("pending draft already processed or expired")):
            self.pending.fail_confirmable_with = failure

            with self.assertRaises(AuthorizationError):
                await self.handlers._dispatch_callback(event, 42, "p:n:7")

            self.assertEqual(self.repository.set_states, [])
        self.assertEqual(self.pending.confirmable_calls, [(7, 42), (7, 42)])

    async def test_custom_name_is_trimmed_and_must_be_between_one_and_one_hundred_characters(self) -> None:
        message = forwarded_message(text="  自定义名称  ")
        self.handlers._show_draft = AsyncMock()

        await self.handlers._handle_state(message, 42, "await_pending_name", {"pending_id": 7})

        self.assertEqual(self.pending.confirm_calls, [(7, 42, "自定义名称")])
        self.assertEqual(self.repository.cleared, [42])
        self.handlers._show_draft.assert_awaited_once_with(message, 42, 11)
        with self.assertRaisesRegex(ValueError, "1 到 100"):
            await self.handlers._handle_state(forwarded_message(text=" "), 42, "await_pending_name", {"pending_id": 7})
        with self.assertRaisesRegex(ValueError, "1 到 100"):
            await self.handlers._handle_state(forwarded_message(text="x" * 101), 42, "await_pending_name", {"pending_id": 7})

    async def test_naming_confirmation_failure_clears_state_for_retry_from_confirmation_menu(self) -> None:
        self.handlers._show_draft = AsyncMock()
        for failure in (AuthorizationError("pending draft already processed or expired"), ResourceLimitError("草稿数量已达上限")):
            self.repository.conversations[42] = ("await_pending_name", {"pending_id": 7})
            self.pending.fail_confirm_with = failure
            message = forwarded_message(text="名称")

            await self.handlers._handle_state(message, 42, "await_pending_name", {"pending_id": 7})

            self.assertNotIn(42, self.repository.conversations)
            callbacks = [button.callback_data for row in message.answers[-1][1].inline_keyboard for button in row]
            self.assertEqual(callbacks, ["p:s:7", "p:n:7", "p:x:7"])
        self.handlers._show_draft.assert_not_awaited()

    async def test_cancel_naming_keeps_pending_draft_and_reopens_confirmation_actions(self) -> None:
        self.repository.conversations[42] = ("await_pending_name", {"pending_id": 7})
        message = forwarded_message(text="/cancel")

        await self.handlers.on_private_message(message)

        self.assertEqual(self.pending.discard_calls, [])
        callbacks = [button.callback_data for row in message.answers[-1][1].inline_keyboard for button in row]
        self.assertEqual(callbacks, ["p:s:7", "p:n:7", "p:x:7"])
        self.assertNotIn(42, self.repository.conversations)

    async def test_quota_error_leaves_confirmation_callback_usable(self) -> None:
        self.pending.fail_confirm_with = ResourceLimitError("草稿数量已达上限")
        event = SimpleNamespace(answer=AsyncMock())

        await self.handlers.on_callback(SimpleNamespace(from_user=SimpleNamespace(id=42, full_name="Alice"), data="p:s:7", answer=event.answer))

        self.assertEqual(self.pending.confirm_calls, [(7, 42, None)])
        event.answer.assert_awaited_once_with("草稿数量已达上限", show_alert=True)

    async def test_permission_unavailable_diagnostics_are_not_exposed_to_user(self) -> None:
        self.repository.conversations[42] = ("await_channel", {})
        self.handlers.channels = SimpleNamespace(
            bind=AsyncMock(
                side_effect=PermissionUnavailable(
                    "TelegramForbiddenError: Telegram server says - bot was kicked"
                )
            )
        )
        message = forwarded_message(text="-1007")

        await self.handlers.on_private_message(message)

        self.assertEqual(
            message.answers[-1][0],
            "操作失败：暂时无法确认频道管理员权限，请稍后重试",
        )
        self.assertNotIn("TelegramForbiddenError", message.answers[-1][0])


class BatchButtonHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.drafts = FakeDraftService()
        self.pending = FakePendingDrafts()
        self.settings = SimpleNamespace(
            conversation_timeout_seconds=900,
            max_drafts_per_user=50,
            max_slots_per_channel=10,
            operator_user_ids=frozenset(),
        )
        self.handlers = BotHandlers(
            SimpleNamespace(),
            self.repository,
            self.drafts,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            FakeGateway(),
            self.settings,
            pending_drafts=self.pending,
        )
        self.draft = Draft(
            11,
            42,
            "按钮草稿",
            DraftRevision(12, 1, (ContentItem(text="正文"),), ()),
        )
        self.repository.get_draft = AsyncMock(return_value=self.draft)

    async def test_invalid_batch_does_not_update_buttons(self) -> None:
        message = forwarded_message(text="官网 | https://example.com | 1\n坏行")

        with self.assertRaisesRegex(ValueError, "第 2 行"):
            await self.handlers._handle_state(message, 42, "await_button", {"draft_id": 11})

        self.drafts.update_buttons.assert_not_awaited()

    async def test_valid_batch_updates_the_complete_combined_layout_once(self) -> None:
        self.draft = Draft(
            11,
            42,
            "按钮草稿",
            DraftRevision(12, 1, (ContentItem(text="正文"),), ()),
        )
        self.repository.get_draft.return_value = self.draft
        message = forwarded_message(text="官网 | https://example.com | 1\n客服 | tg://resolve?domain=example | 1")

        await self.handlers._handle_state(message, 42, "await_button", {"draft_id": 11})

        self.drafts.update_buttons.assert_awaited_once()
        buttons = self.drafts.update_buttons.await_args.args[2]
        self.assertEqual([(button.text, button.row, button.column) for button in buttons], [("官网", 0, 0), ("客服", 0, 1)])
        self.assertEqual(message.answers[-1][0], "已添加 2 个按钮。")


class ChannelSlotNameHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.repository.get_channel = AsyncMock(
            return_value={"title": "News", "status": "active", "enabled": 1, "silent": 0, "refresh_delay_seconds": 10}
        )
        revision = DraftRevision(12, 2, (ContentItem(text="正文"),))
        self.repository.list_channel_slots = AsyncMock(
            return_value=[SlotSnapshot(-1007, 1, revision, True, "首页入口", True)]
        )
        self.channels = SimpleNamespace(rename_slot=AsyncMock())
        self.settings = SimpleNamespace(
            conversation_timeout_seconds=900,
            max_drafts_per_user=50,
            max_slots_per_channel=3,
            operator_user_ids=frozenset(),
        )
        self.handlers = BotHandlers(
            SimpleNamespace(),
            self.repository,
            FakeDraftService(),
            self.channels,
            SimpleNamespace(assert_user_can_manage=AsyncMock()),
            SimpleNamespace(),
            FakeGateway(),
            self.settings,
            pending_drafts=FakePendingDrafts(),
        )

    async def test_channel_page_renders_slot_names_and_truncates_occupied_slot_buttons(self) -> None:
        self.handlers._show = AsyncMock()

        await self.handlers._show_channel(SimpleNamespace(), 42, -1007)

        text = self.handlers._show.await_args.args[1]
        rows = self.handlers._show.await_args.args[2]
        labels = [button.text for row in rows for button in row]
        self.assertIn("1. 首页入口｜已启用｜版本 2", text)
        self.assertIn("1 首页入口", labels)
        self.assertIn("改名 1号", labels)

    async def test_rename_callback_sets_slot_name_state(self) -> None:
        self.handlers._show = AsyncMock()

        await self.handlers._dispatch_callback(SimpleNamespace(), 42, "c:slot_name:-1007:1")

        self.assertEqual(self.repository.set_states[-1][:3], (42, "await_slot_name", {"channel_id": -1007, "slot": 1}))

    async def test_slot_name_rejects_whitespace_and_submits_trimmed_name_without_refresh(self) -> None:
        with self.assertRaisesRegex(ValueError, "1 到 100"):
            await self.handlers._handle_state(forwarded_message(text="   "), 42, "await_slot_name", {"channel_id": -1007, "slot": 1})

        self.handlers._show_channel = AsyncMock()
        await self.handlers._handle_state(forwarded_message(text=" 首页入口 "), 42, "await_slot_name", {"channel_id": -1007, "slot": 1})

        self.channels.rename_slot.assert_awaited_once_with(-1007, 1, "首页入口", 42)
        self.handlers._show_channel.assert_awaited_once()

    async def test_channel_callbacks_fit_telegram_callback_data_limit(self) -> None:
        channel_id = -1001234567890
        revision = DraftRevision(12, 2, (ContentItem(text="正文"),))
        self.repository.list_channel_slots.return_value = [SlotSnapshot(channel_id, 1, revision, True, "首页入口", True)]
        self.handlers._show = AsyncMock()

        await self.handlers._show_channel(SimpleNamespace(), 42, channel_id)

        rows = self.handlers._show.await_args.args[2]
        callback_data = [button.callback_data for row in rows for button in row]
        self.assertIn(f"c:slot_name:{channel_id}:1", callback_data)
        self.assertTrue(all(len(data.encode("utf-8")) <= 64 for data in callback_data))


class AiogramMessageConversionTests(unittest.TestCase):
    def test_photo_message_extracts_largest_file_id_and_media_group(self) -> None:
        entity = SimpleNamespace(model_dump=lambda **kwargs: {"type": "bold", "offset": 0, "length": 3})
        message = SimpleNamespace(
            chat=SimpleNamespace(id=12),
            message_id=34,
            text=None,
            caption="广告",
            entities=None,
            caption_entities=[entity],
            media_group_id="group-1",
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
            video=None,
            animation=None,
            document=None,
            audio=None,
            voice=None,
            sticker=None,
        )

        incoming = message_to_incoming(message)

        self.assertEqual(incoming.source_chat_id, 12)
        self.assertEqual(incoming.source_message_id, 34)
        self.assertEqual(incoming.media_kind, "photo")
        self.assertEqual(incoming.file_id, "large")
        self.assertEqual(incoming.grouped_id, "group-1")
        self.assertEqual(json.loads(incoming.formatting_entities_json)[0]["type"], "bold")

    def test_plain_text_has_no_file_id(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=12),
            message_id=35,
            text="hello",
            caption=None,
            entities=[],
            caption_entities=None,
            media_group_id=None,
            photo=None,
            video=None,
            animation=None,
            document=None,
            audio=None,
            voice=None,
            sticker=None,
        )
        incoming = message_to_incoming(message)
        self.assertEqual(incoming.text, "hello")
        self.assertIsNone(incoming.file_id)


if __name__ == "__main__":
    unittest.main()
