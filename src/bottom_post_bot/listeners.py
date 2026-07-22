from __future__ import annotations

from aiogram.enums import ContentType


_USER_CONTENT_TYPES = frozenset(
    {
        ContentType.TEXT,
        ContentType.ANIMATION,
        ContentType.AUDIO,
        ContentType.DOCUMENT,
        ContentType.LIVE_PHOTO,
        ContentType.PAID_MEDIA,
        ContentType.PHOTO,
        ContentType.STICKER,
        ContentType.STORY,
        ContentType.VIDEO,
        ContentType.VIDEO_NOTE,
        ContentType.VOICE,
        ContentType.CHECKLIST,
        ContentType.CONTACT,
        ContentType.DICE,
        ContentType.GAME,
        ContentType.POLL,
        ContentType.VENUE,
        ContentType.LOCATION,
        ContentType.INVOICE,
        ContentType.GIVEAWAY,
        ContentType.RICH_MESSAGE,
    }
)


def _is_user_content(event) -> bool:
    """Accept only known user message types; unknown and service types cannot trigger a refresh."""
    content_type = getattr(event, "content_type", None)
    if content_type is None:
        # Compatibility with the legacy listener shape, which has no aiogram content-type field.
        return True
    value = getattr(content_type, "value", content_type)
    return value in _USER_CONTENT_TYPES


class ChannelListener:
    def __init__(self, repository, scheduler, *, bot_user_id: int | None = None) -> None:
        self.repository = repository
        self.scheduler = scheduler
        self.bot_user_id = bot_user_id

    async def handle(self, event) -> None:
        wrapped = getattr(event, "message", None)
        if getattr(event, "out", False) or getattr(wrapped, "action", None) is not None:
            return
        author = getattr(event, "from_user", None)
        if author is not None and self.bot_user_id is not None and int(getattr(author, "id", 0)) == self.bot_user_id:
            return
        if not _is_user_content(event):
            return
        chat = getattr(event, "chat", None)
        channel_id = int(chat.id if chat is not None else event.chat_id)
        message_id = int(getattr(event, "message_id", getattr(event, "id", 0)))
        delay = await self.repository.channel_refresh_delay(channel_id)
        if delay is None:
            return
        if await self.repository.is_current_sent_message(channel_id, message_id):
            return
        await self.scheduler.request(channel_id, f"channel-message:{message_id}", delay)
