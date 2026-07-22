from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated

from .channels import ChannelService
from .analytics import AnalyticsService
from .notifications import TelegramAdminNotifier
from .permissions import PermissionUnavailable
from .repositories import Repository, ResourceLimitError


logger = logging.getLogger(__name__)

_ADMIN_STATUSES = frozenset({"administrator", "creator", "owner"})
_ACCESS_LOSS_STATUSES = frozenset({"left", "kicked", "member", "restricted"})


def _status_name(status: object) -> str:
    value = getattr(status, "value", status)
    name = str(value).lower()
    return name.rsplit(".", 1)[-1]


def _user_name(user) -> str:
    return getattr(user, "full_name", None) or getattr(user, "username", None) or str(user.id)


class ChatMembershipService:
    def __init__(
        self,
        repository: Repository,
        channels: ChannelService,
        notifier: TelegramAdminNotifier,
        *,
        storage_channel_id: int,
        analytics: AnalyticsService | None = None,
    ) -> None:
        self.repository = repository
        self.channels = channels
        self.notifier = notifier
        self.storage_channel_id = storage_channel_id
        self.analytics = analytics

    async def handle(self, event: ChatMemberUpdated) -> None:
        chat = event.chat
        chat_type = _status_name(chat.type)
        if chat_type not in {"channel", "supergroup"} or int(chat.id) == self.storage_channel_id:
            return

        actor = event.from_user
        channel_id = int(chat.id)
        old_status = _status_name(event.old_chat_member.status)
        new_status = _status_name(event.new_chat_member.status)

        if new_status in _ACCESS_LOSS_STATUSES:
            if not await self.repository.has_channel(channel_id):
                return
            await self.repository.upsert_user(actor.id, _user_name(actor))
            await self.repository.pause_channel(channel_id, f"bot membership changed from {old_status} to {new_status}")
            await self.repository.audit(
                channel_id,
                actor.id,
                "channel.bot_access_lost",
                {"old_status": old_status, "new_status": new_status},
            )
            if self.analytics is not None:
                occurred_at = getattr(event, "date", datetime.now(UTC))
                await self.analytics.mark_permission_gap(channel_id, occurred_at, occurred_at, "bot access lost")
            return

        if new_status not in _ADMIN_STATUSES:
            return

        existing_configuration = await self.repository.has_channel_configuration(channel_id)
        await self.repository.upsert_user(actor.id, _user_name(actor))
        await self.repository.upsert_channel(
            channel_id,
            getattr(chat, "title", None) or str(channel_id),
            getattr(chat, "username", None),
            self.channels.default_refresh_delay,
            chat_type,
        )
        try:
            _, created = await self.channels.bind_with_result(actor.id, channel_id)
        except (PermissionError, ResourceLimitError, TelegramAPIError) as exc:
            await self.repository.audit(
                channel_id,
                actor.id,
                "channel.auto_bind_failed",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            if existing_configuration and not isinstance(exc, (PermissionUnavailable, TelegramAPIError)):
                try:
                    capabilities = await self.channels.permissions.gateway.bot_capabilities(channel_id)
                except TelegramAPIError as capability_error:
                    logger.warning(
                        "Could not recheck bot channel capabilities after auto-bind failure",
                        extra={
                            "channel_id": channel_id,
                            "error_type": type(capability_error).__name__,
                        },
                    )
                else:
                    if not capabilities.ready:
                        await self.repository.pause_channel(channel_id, "bot lacks required channel capabilities")
            return

        if self.analytics is not None:
            await self.analytics.initialize_channel(channel_id, getattr(event, "date", datetime.now(UTC)))
        if created:
            await self.repository.audit(channel_id, actor.id, "channel.auto_bind", {})
            label = "超级群组" if chat_type == "supergroup" else "频道"
            await self.notifier.notify_user(actor.id, f"已自动绑定{label}“{getattr(chat, 'title', None) or channel_id}”（ID: {channel_id}）。")

    async def reconcile_managed_chats(self, activated_at: datetime | None = None) -> int:
        """Refresh persisted Telegram chat identities when the process starts."""
        reconciled = await self.channels.reconcile_managed_chats()
        if self.analytics is not None:
            timestamp = activated_at or datetime.now(UTC)
            for channel_id in reconciled.failed_ids:
                await self.analytics.mark_permission_gap(channel_id, timestamp, timestamp, "chat reconciliation failed")
            for identity in reconciled.identities:
                await self.analytics.initialize_channel(identity.id, timestamp)
        return len(reconciled.identities)
