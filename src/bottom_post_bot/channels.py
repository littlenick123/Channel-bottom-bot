from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError

from .permissions import PermissionDenied, PermissionService
from .repositories import Repository

if TYPE_CHECKING:
    from .analytics import AnalyticsService


logger = logging.getLogger(__name__)


class UnsupportedChatTypeError(ValueError):
    """Telegram resolved a chat type this bot does not manage."""


@dataclass(frozen=True, slots=True)
class ChannelIdentity:
    id: int
    title: str
    username: str | None
    chat_type: str = "channel"
    stats_baseline_available: bool | None = None


@dataclass(frozen=True, slots=True)
class ReconciledChats:
    identities: tuple[ChannelIdentity, ...]
    failed_ids: tuple[int, ...]


class ChannelService:
    def __init__(
        self,
        repository: Repository,
        permissions: PermissionService,
        *,
        max_channels: int,
        max_slots: int,
        storage_channel_id: int,
        default_refresh_delay: int = 10,
        analytics: "AnalyticsService | None" = None,
    ) -> None:
        self.repository = repository
        self.permissions = permissions
        self.max_channels = max_channels
        self.max_slots = max_slots
        self.storage_channel_id = storage_channel_id
        self.default_refresh_delay = default_refresh_delay
        self.analytics = analytics

    async def bind(self, user_id: int, reference: str | int) -> ChannelIdentity:
        channel, _ = await self.bind_with_result(user_id, reference)
        return channel

    async def bind_with_result(self, user_id: int, reference: str | int) -> tuple[ChannelIdentity, bool]:
        try:
            referenced_channel_id = int(str(reference).strip())
        except ValueError:
            referenced_channel_id = None
        if referenced_channel_id == self.storage_channel_id:
            raise PermissionDenied("私密存储频道不能绑定为目标频道")
        channel = await self.permissions.assert_can_bind(user_id, reference)
        if channel.id == self.storage_channel_id:
            raise PermissionDenied("私密存储频道不能绑定为目标频道")
        if self.analytics is not None:
            activated_at = datetime.now(UTC)
            created, _ = await self.repository.bind_manager_with_analytics(
                user_id,
                "",
                channel.id,
                channel.title,
                channel.username,
                self.default_refresh_delay,
                channel.chat_type,
                self.max_channels,
                self.analytics._timestamp(activated_at),
                self.analytics.local_date(activated_at).isoformat(),
            )
            baseline = await self.analytics.refresh_current_count(channel.id, activated_at)
            await self.analytics.end_permission_interruption(channel.id, activated_at)
            channel = replace(channel, stats_baseline_available=baseline is not None)
        else:
            await self.repository.upsert_channel(
                channel.id, channel.title, channel.username, self.default_refresh_delay, channel.chat_type
            )
            created = await self.repository.bind_manager(user_id, channel.id, self.max_channels)
        if created:
            await self.repository.audit(channel.id, user_id, "channel.bind", {})
        return channel, created

    async def reconcile_managed_chats(self) -> ReconciledChats:
        """Re-read Telegram identities for managed chats, retaining their configured options."""
        reconciled: list[ChannelIdentity] = []
        failed_ids: list[int] = []
        for row in await self.repository.list_managed_channels():
            channel_id = int(row["id"])
            try:
                channel = await self.permissions.gateway.resolve_channel(channel_id)
                capabilities = await self.permissions.gateway.bot_capabilities(channel_id)
                if not capabilities.ready:
                    raise PermissionDenied("bot lacks required channel capabilities")
            except (TelegramAPIError, UnsupportedChatTypeError, PermissionDenied) as exc:
                failed_ids.append(channel_id)
                logger.warning(
                    "Managed chat reconciliation failed",
                    extra={"channel_id": channel_id, "error_type": type(exc).__name__},
                )
                continue
            if channel.id == self.storage_channel_id:
                continue
            await self.repository.upsert_channel(
                channel.id, channel.title, channel.username, int(row["refresh_delay_seconds"]), channel.chat_type
            )
            reconciled.append(channel)
        return ReconciledChats(tuple(reconciled), tuple(failed_ids))

    async def assign_slot(self, channel_id: int, slot_number: int, revision_id: int, actor_id: int) -> None:
        await self.permissions.assert_user_can_manage(actor_id, channel_id)
        draft_name = await self.repository.owned_draft_name_for_revision(revision_id, actor_id)
        if draft_name is None:
            raise PermissionDenied("只能把自己的个人草稿发布到频道槽位")
        await self.repository.assign_slot(channel_id, slot_number, revision_id, actor_id, self.max_slots, draft_name)

    async def rename_slot(self, channel_id: int, slot_number: int, display_name: str, actor_id: int) -> None:
        name = display_name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("槽位名称长度必须为 1 到 100 个字符")
        await self.permissions.assert_user_can_manage(actor_id, channel_id)
        await self.repository.rename_slot(channel_id, slot_number, name, actor_id)

    async def clear_slot(self, channel_id: int, slot_number: int, actor_id: int) -> None:
        await self.permissions.assert_user_can_manage(actor_id, channel_id)
        await self.repository.clear_slot(channel_id, slot_number, actor_id)

    async def set_slot_enabled(self, channel_id: int, slot_number: int, enabled: bool, actor_id: int) -> None:
        await self.permissions.assert_user_can_manage(actor_id, channel_id)
        await self.repository.set_slot_enabled(channel_id, slot_number, enabled, actor_id)

    async def move_slot(
        self, channel_id: int, source_number: int, target_number: int, actor_id: int
    ) -> None:
        await self.permissions.assert_user_can_manage(actor_id, channel_id)
        await self.repository.move_slot(
            channel_id, source_number, target_number, actor_id, self.max_slots
        )

    async def update_options(self, channel_id: int, actor_id: int, **options) -> None:
        await self.permissions.assert_user_can_manage(actor_id, channel_id)
        await self.repository.set_channel_options(channel_id, actor_id, **options)

    async def leave(self, channel_id: int, actor_id: int) -> None:
        await self.permissions.assert_user_can_manage(actor_id, channel_id)
        if self.analytics is not None and len(await self.repository.list_manager_ids(channel_id)) == 1:
            await self.analytics.begin_permission_interruption(
                channel_id, datetime.now(UTC), "last manager unbound"
            )
        await self.repository.unbind_manager(actor_id, channel_id)
