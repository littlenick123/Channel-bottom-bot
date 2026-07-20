from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


class TelegramAdminNotifier:
    def __init__(self, client, repository, permission_gateway) -> None:
        self.client = client
        self.repository = repository
        self.permission_gateway = permission_gateway

    async def notify_user(self, user_id: int, text: str) -> bool:
        try:
            await self.client.send_message(chat_id=user_id, text=text)
        except TelegramAPIError as exc:
            logger.warning(
                "Could not notify user",
                extra={"user_id": user_id, "error_type": type(exc).__name__},
            )
            return False
        return True

    async def notify_channel_admins(self, channel_id: int, text: str) -> None:
        channel = await self.repository.get_channel(channel_id)
        title = channel["title"] if channel else str(channel_id)
        for user_id in await self.repository.list_manager_ids(channel_id):
            try:
                if not await self.permission_gateway.user_is_admin(channel_id, user_id):
                    await self.repository.unbind_manager(user_id, channel_id)
                    continue
                await self.client.send_message(
                    chat_id=user_id,
                    text=f"频道“{title}”：{text}\n请打开频道配置检查后恢复。",
                )
            except Exception as exc:
                logger.warning("Could not notify channel manager", extra={"channel_id": channel_id, "user_id": user_id, "error_type": type(exc).__name__})
