from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    MessageEntity,
)

from .channels import ChannelIdentity
from .domain import ButtonSpec, ContentItem
from .drafts import IncomingContent
from .permissions import BotCapabilities, PermissionUnavailable
from .publisher import FloodWaitSignal, PermanentPublishError


TRANSIENT_ERRORS = (TelegramNetworkError, TelegramServerError)
ADMIN_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, "creator", "owner", "administrator"}


def _message_id(value) -> int:
    return int(getattr(value, "message_id", getattr(value, "id", 0)))


def _entities(value: str) -> list[MessageEntity]:
    if not value:
        return []
    return [MessageEntity.model_validate(entity) for entity in json.loads(value)]


class BotApiPermissionGateway:
    def __init__(self, bot) -> None:
        self.bot = bot
        self._bot_id: int | None = None

    async def resolve_channel(self, reference: str | int) -> ChannelIdentity:
        chat = await self.bot.get_chat(reference)
        chat_type = str(getattr(chat.type, "value", chat.type)).lower()
        if chat_type not in {"channel", "supergroup"}:
            raise ValueError("目标不是 Telegram 频道或超级群组")
        return ChannelIdentity(int(chat.id), str(chat.title or chat.id), getattr(chat, "username", None), chat_type)

    async def user_is_admin(self, channel_id: int, user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(channel_id, user_id)
            return member.status in ADMIN_STATUSES
        except TelegramAPIError as exc:
            raise PermissionUnavailable(f"{type(exc).__name__}: {exc}") from exc

    async def bot_capabilities(self, channel_id: int) -> BotCapabilities:
        if self._bot_id is None:
            self._bot_id = int((await self.bot.get_me()).id)
        try:
            member = await self.bot.get_chat_member(channel_id, self._bot_id)
        except (TelegramForbiddenError, TelegramBadRequest):
            return BotCapabilities(False, False, False)
        chat = await self.bot.get_chat(channel_id)
        chat_type = str(getattr(chat.type, "value", chat.type)).lower()
        creator = member.status in {ChatMemberStatus.CREATOR, "creator", "owner"}
        admin = member.status in ADMIN_STATUSES
        if chat_type == "channel":
            can_send = creator or bool(getattr(member, "can_post_messages", False))
        else:
            can_send = admin and getattr(member, "can_send_messages", True) is not False
        return BotCapabilities(
            is_admin=admin,
            can_send=can_send,
            can_delete=creator or bool(getattr(member, "can_delete_messages", False)),
        )


class BotApiGateway:
    def __init__(self, bot, storage_channel_id: int) -> None:
        self.bot = bot
        self.storage_channel_id = storage_channel_id

    async def get_member_count(self, chat_id: int) -> int:
        try:
            return int(await self.bot.get_chat_member_count(chat_id))
        except TelegramRetryAfter as exc:
            raise FloodWaitSignal(exc.retry_after) from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentPublishError(f"无法获取成员数：{exc}") from exc

    async def copy_messages(self, messages: Sequence[IncomingContent]) -> list[int]:
        if not messages:
            return []
        if len({message.source_chat_id for message in messages}) != 1:
            raise ValueError("同一草稿批次的消息必须来自同一会话")
        try:
            copied = await self.bot.copy_messages(
                chat_id=self.storage_channel_id,
                from_chat_id=messages[0].source_chat_id,
                message_ids=[message.source_message_id for message in messages],
                disable_notification=True,
            )
            return [_message_id(item) for item in copied]
        except TelegramRetryAfter as exc:
            raise FloodWaitSignal(exc.retry_after) from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentPublishError(f"存储频道复制失败：{exc}") from exc

    async def preview_storage_messages(self, user_id: int, message_ids: Sequence[int]) -> list[int]:
        if not message_ids:
            return []
        try:
            copied = await self.bot.copy_messages(
                chat_id=user_id,
                from_chat_id=self.storage_channel_id,
                message_ids=list(message_ids),
                disable_notification=True,
            )
            return [_message_id(item) for item in copied]
        except TelegramRetryAfter as exc:
            raise FloodWaitSignal(exc.retry_after) from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentPublishError(f"私聊预览复制失败：{exc}") from exc

    async def delete_messages(self, channel_id: int, message_ids: list[int]) -> None:
        if not message_ids:
            return
        try:
            await self.bot.delete_messages(chat_id=channel_id, message_ids=message_ids)
        except TelegramRetryAfter as exc:
            raise FloodWaitSignal(exc.retry_after) from exc
        except TelegramBadRequest as exc:
            if "not found" in str(exc).lower() or "can't be deleted" in str(exc).lower():
                return
            raise PermanentPublishError(f"无法删除旧置底帖：{exc}") from exc
        except TelegramForbiddenError as exc:
            raise PermanentPublishError(f"无法删除旧置底帖：{exc}") from exc

    async def delete_storage_messages(self, message_ids: list[int]) -> None:
        await self.delete_messages(self.storage_channel_id, message_ids)

    async def send_content(
        self,
        channel_id: int,
        item: ContentItem,
        buttons: Sequence[ButtonSpec],
        silent: bool,
    ) -> list[int]:
        common = {
            "chat_id": channel_id,
            "disable_notification": silent,
            "reply_markup": self._build_buttons(buttons),
        }
        entities = _entities(item.formatting_entities_json)
        try:
            if not item.media_kind:
                sent = await self.bot.send_message(
                    **common,
                    text=item.text or "\u200b",
                    entities=entities or None,
                    link_preview_options=LinkPreviewOptions(is_disabled=False),
                )
            else:
                sent = await self._send_media(item, entities, common)
            return [_message_id(sent)]
        except TelegramRetryAfter as exc:
            raise FloodWaitSignal(exc.retry_after) from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentPublishError(f"频道发帖失败：{exc}") from exc
        except TRANSIENT_ERRORS:
            raise

    async def _send_media(self, item: ContentItem, entities: list[MessageEntity], common: dict):
        if not item.telegram_file_id:
            raise PermanentPublishError("媒体草稿缺少 Bot API file_id")
        caption = item.text or None
        kind = item.media_kind.lower()
        if kind == "photo":
            return await self.bot.send_photo(**common, photo=item.telegram_file_id, caption=caption, caption_entities=entities or None)
        if kind == "video":
            return await self.bot.send_video(**common, video=item.telegram_file_id, caption=caption, caption_entities=entities or None)
        if kind == "animation":
            return await self.bot.send_animation(**common, animation=item.telegram_file_id, caption=caption, caption_entities=entities or None)
        if kind == "audio":
            return await self.bot.send_audio(**common, audio=item.telegram_file_id, caption=caption, caption_entities=entities or None)
        if kind == "voice":
            return await self.bot.send_voice(**common, voice=item.telegram_file_id, caption=caption, caption_entities=entities or None)
        if kind == "sticker":
            common.pop("reply_markup", None)
            return await self.bot.send_sticker(**common, sticker=item.telegram_file_id)
        return await self.bot.send_document(**common, document=item.telegram_file_id, caption=caption, caption_entities=entities or None)

    async def send_content_group(
        self,
        channel_id: int,
        items: Sequence[ContentItem],
        buttons: Sequence[ButtonSpec],
        silent: bool,
    ) -> list[int]:
        try:
            media = [self._input_media(item) for item in items]
            sent = await self.bot.send_media_group(
                chat_id=channel_id,
                media=media,
                disable_notification=silent,
            )
            ids = [_message_id(message) for message in sent]
            if buttons:
                button_message = await self.bot.send_message(
                    chat_id=channel_id,
                    text="\u200b",
                    reply_markup=self._build_buttons(buttons),
                    disable_notification=silent,
                )
                ids.append(_message_id(button_message))
            return ids
        except TelegramRetryAfter as exc:
            raise FloodWaitSignal(exc.retry_after) from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentPublishError(f"频道相册发布失败：{exc}") from exc

    @staticmethod
    def _input_media(item: ContentItem):
        if not item.telegram_file_id:
            raise PermanentPublishError("相册媒体缺少 Bot API file_id")
        entities = _entities(item.formatting_entities_json) or None
        kind = item.media_kind.lower()
        kwargs = {"media": item.telegram_file_id, "caption": item.text or None, "caption_entities": entities}
        if kind == "photo":
            return InputMediaPhoto(**kwargs)
        if kind == "video":
            return InputMediaVideo(**kwargs)
        if kind == "audio":
            return InputMediaAudio(**kwargs)
        return InputMediaDocument(**kwargs)

    @staticmethod
    def _build_buttons(buttons: Sequence[ButtonSpec]) -> InlineKeyboardMarkup | None:
        if not buttons:
            return None
        rows: defaultdict[int, list[ButtonSpec]] = defaultdict(list)
        for button in buttons:
            rows[button.row].append(button)
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=button.text, url=button.url) for button in sorted(rows[row], key=lambda value: value.column)]
                for row in sorted(rows)
            ]
        )
