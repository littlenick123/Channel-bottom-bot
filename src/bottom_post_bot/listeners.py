from __future__ import annotations


class ChannelListener:
    def __init__(self, repository, scheduler) -> None:
        self.repository = repository
        self.scheduler = scheduler

    async def handle(self, event) -> None:
        wrapped = getattr(event, "message", None)
        if getattr(event, "out", False) or getattr(wrapped, "action", None) is not None:
            return
        author = getattr(event, "from_user", None)
        if author is not None and getattr(author, "is_bot", False):
            return
        if any(
            getattr(event, field, None) is not None
            for field in ("new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo", "group_chat_created", "supergroup_chat_created", "channel_chat_created", "pinned_message", "migrate_to_chat_id", "migrate_from_chat_id")
        ):
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
