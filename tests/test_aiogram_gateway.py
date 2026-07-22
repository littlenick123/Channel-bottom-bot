import unittest
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe

from bottom_post_bot.aiogram_gateway import BotApiGateway, BotApiPermissionGateway
from bottom_post_bot.domain import ButtonSpec, ContentItem
from bottom_post_bot.drafts import IncomingContent
from bottom_post_bot.permissions import PermissionUnavailable
from bottom_post_bot.publisher import FloodWaitSignal, PermanentPublishError


class FakeBot:
    def __init__(self) -> None:
        self.copy_calls = []
        self.message_calls = []
        self.photo_calls = []
        self.album_calls = []
        self.delete_calls = []
        self.copy_error = None
        self.user_member_status = "administrator"
        self.user_member_error: Exception | None = None
        self.chat_type = "channel"
        self.member_count = 42
        self.member_count_error: Exception | None = None

    async def copy_messages(self, **kwargs):
        self.copy_calls.append(kwargs)
        if self.copy_error:
            raise self.copy_error
        return [SimpleNamespace(message_id=700 + index) for index, _ in enumerate(kwargs["message_ids"])]

    async def send_message(self, **kwargs):
        self.message_calls.append(kwargs)
        return SimpleNamespace(message_id=901)

    async def send_photo(self, **kwargs):
        self.photo_calls.append(kwargs)
        return SimpleNamespace(message_id=902)

    async def send_media_group(self, **kwargs):
        self.album_calls.append(kwargs)
        return [SimpleNamespace(message_id=910 + index) for index, _ in enumerate(kwargs["media"])]

    async def delete_messages(self, **kwargs):
        self.delete_calls.append(kwargs)
        return True

    async def get_chat(self, reference):
        return SimpleNamespace(id=-1007, title="News", username="news", type=self.chat_type)

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id, user_id):
        if user_id == 999:
            return SimpleNamespace(
                status="administrator",
                can_post_messages=True,
                can_delete_messages=True,
            )
        if self.user_member_error is not None:
            raise self.user_member_error
        return SimpleNamespace(status=self.user_member_status)

    async def get_chat_member_count(self, chat_id):
        if self.member_count_error is not None:
            raise self.member_count_error
        return self.member_count


class BotApiGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_messages_stores_album_in_one_bot_api_call(self) -> None:
        bot = FakeBot()
        gateway = BotApiGateway(bot, storage_channel_id=-10050)
        incoming = [
            IncomingContent(-1007, 1, "caption", "photo", "album", "[]", "photo-file-1"),
            IncomingContent(-1007, 2, None, "photo", "album", "[]", "photo-file-2"),
        ]

        ids = await gateway.copy_messages(incoming)

        self.assertEqual(ids, [700, 701])
        self.assertEqual(len(bot.copy_calls), 1)
        self.assertEqual(bot.copy_calls[0]["message_ids"], [1, 2])

    async def test_preview_storage_messages_copies_stored_album_to_private_chat(self) -> None:
        bot = FakeBot()
        gateway = BotApiGateway(bot, storage_channel_id=-10050)

        ids = await gateway.preview_storage_messages(42, [700, 701])

        self.assertEqual(ids, [700, 701])
        self.assertEqual(
            bot.copy_calls,
            [
                {
                    "chat_id": 42,
                    "from_chat_id": -10050,
                    "message_ids": [700, 701],
                    "disable_notification": True,
                }
            ],
        )

    async def test_preview_storage_messages_translates_retry_after(self) -> None:
        bot = FakeBot()
        bot.copy_error = TelegramRetryAfter(GetMe(), "slow down", retry_after=3)

        with self.assertRaises(FloodWaitSignal) as raised:
            await BotApiGateway(bot, storage_channel_id=-10050).preview_storage_messages(42, [700])

        self.assertEqual(raised.exception.seconds, 3)

    async def test_preview_storage_messages_translates_private_preview_errors(self) -> None:
        bot = FakeBot()
        bot.copy_error = TelegramBadRequest(GetMe(), "chat not found")

        with self.assertRaisesRegex(PermanentPublishError, "私聊预览"):
            await BotApiGateway(bot, storage_channel_id=-10050).preview_storage_messages(42, [700])

    async def test_preview_storage_messages_translates_forbidden_private_preview_errors(self) -> None:
        bot = FakeBot()
        bot.copy_error = TelegramForbiddenError(GetMe(), "bot was blocked by the user")

        with self.assertRaisesRegex(PermanentPublishError, "私聊预览"):
            await BotApiGateway(bot, storage_channel_id=-10050).preview_storage_messages(42, [700])

    async def test_send_photo_uses_persistent_file_id_and_url_keyboard(self) -> None:
        bot = FakeBot()
        gateway = BotApiGateway(bot, storage_channel_id=-10050)
        item = ContentItem(
            text="caption",
            storage_message_id=701,
            media_kind="photo",
            telegram_file_id="photo-file",
        )
        buttons = [ButtonSpec("官网", "https://example.com", 0, 0)]

        ids = await gateway.send_content(-1007, item, buttons, silent=True)

        self.assertEqual(ids, [902])
        self.assertEqual(bot.photo_calls[0]["photo"], "photo-file")
        self.assertTrue(bot.photo_calls[0]["disable_notification"])
        self.assertEqual(bot.photo_calls[0]["reply_markup"].inline_keyboard[0][0].url, "https://example.com")

    async def test_send_album_uses_media_group(self) -> None:
        bot = FakeBot()
        gateway = BotApiGateway(bot, storage_channel_id=-10050)
        items = [
            ContentItem(text="caption", storage_message_id=1, media_kind="photo", grouped_id="g", telegram_file_id="p1"),
            ContentItem(storage_message_id=2, media_kind="photo", grouped_id="g", telegram_file_id="p2"),
        ]

        ids = await gateway.send_content_group(-1007, items, (), silent=False)

        self.assertEqual(ids, [910, 911])
        self.assertEqual([media.media for media in bot.album_calls[0]["media"]], ["p1", "p2"])

    async def test_permission_gateway_uses_get_chat_member(self) -> None:
        gateway = BotApiPermissionGateway(FakeBot())
        channel = await gateway.resolve_channel("@news")
        self.assertEqual((channel.id, channel.chat_type), (-1007, "channel"))
        self.assertTrue(await gateway.user_is_admin(channel.id, 1))
        self.assertTrue((await gateway.bot_capabilities(channel.id)).ready)

    async def test_permission_gateway_resolves_supergroup_and_rejects_basic_group(self) -> None:
        bot = FakeBot()
        bot.chat_type = "supergroup"
        gateway = BotApiPermissionGateway(bot)

        self.assertEqual((await gateway.resolve_channel(-1007)).chat_type, "supergroup")
        bot.chat_type = "group"
        with self.assertRaisesRegex(ValueError, "群组"):
            await gateway.resolve_channel(-1007)

    async def test_supergroup_bot_capabilities_require_send_and_delete_permissions(self) -> None:
        bot = FakeBot()
        bot.chat_type = "supergroup"

        class Member:
            status = "administrator"
            can_send_messages = False
            can_delete_messages = True

        async def get_chat_member(chat_id, user_id):
            return Member()

        bot.get_chat_member = get_chat_member
        capabilities = await BotApiPermissionGateway(bot).bot_capabilities(-1007)
        self.assertEqual((capabilities.is_admin, capabilities.can_send, capabilities.can_delete), (True, False, True))

    async def test_get_member_count_translates_retry_permanent_and_transient_errors(self) -> None:
        bot = FakeBot()
        gateway = BotApiGateway(bot, storage_channel_id=-10050)
        self.assertEqual(await gateway.get_member_count(-1007), 42)

        bot.member_count_error = TelegramRetryAfter(GetMe(), "slow down", retry_after=3)
        with self.assertRaises(FloodWaitSignal):
            await gateway.get_member_count(-1007)
        bot.member_count_error = TelegramForbiddenError(GetMe(), "bot removed")
        with self.assertRaisesRegex(PermanentPublishError, "成员数"):
            await gateway.get_member_count(-1007)
        transient = TelegramNetworkError(GetMe(), "offline")
        bot.member_count_error = transient
        with self.assertRaises(TelegramNetworkError) as raised:
            await gateway.get_member_count(-1007)
        self.assertIs(raised.exception, transient)

    async def test_permission_gateway_returns_false_for_definite_non_admin_member(self) -> None:
        bot = FakeBot()
        bot.user_member_status = "member"

        self.assertFalse(await BotApiPermissionGateway(bot).user_is_admin(-1007, 1))

    async def test_permission_gateway_surfaces_forbidden_membership_lookup_as_unavailable(self) -> None:
        bot = FakeBot()
        lookup_error = TelegramForbiddenError(GetMe(), "bot was kicked")
        bot.user_member_error = lookup_error

        with self.assertRaises(PermissionUnavailable) as raised:
            await BotApiPermissionGateway(bot).user_is_admin(-1007, 1)
        self.assertIn("TelegramForbiddenError", str(raised.exception))
        self.assertIn("bot was kicked", str(raised.exception))
        self.assertIs(raised.exception.__cause__, lookup_error)

    async def test_permission_gateway_surfaces_ambiguous_bad_request_as_unavailable(self) -> None:
        bot = FakeBot()
        lookup_error = TelegramBadRequest(GetMe(), "member lookup unavailable")
        bot.user_member_error = lookup_error

        with self.assertRaises(PermissionUnavailable) as raised:
            await BotApiPermissionGateway(bot).user_is_admin(-1007, 1)
        self.assertIn("TelegramBadRequest", str(raised.exception))
        self.assertIn("member lookup unavailable", str(raised.exception))
        self.assertIs(raised.exception.__cause__, lookup_error)


if __name__ == "__main__":
    unittest.main()
