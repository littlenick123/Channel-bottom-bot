from __future__ import annotations


class ChannelListener:
    def __init__(self, repository, scheduler) -> None:
        self.repository = repository
        self.scheduler = scheduler

    async def handle(self, event) -> None:
        wrapped = getattr(event, "message", None)
        if getattr(event, "out", False) or getattr(wrapped, "action", None) is not None:
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
