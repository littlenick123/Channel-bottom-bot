from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .repositories import Repository


class PermissionDenied(PermissionError):
    pass


class PermissionUnavailable(PermissionDenied):
    """Raised when Telegram cannot reliably determine current permissions."""

    public_message = "暂时无法确认频道或超级群组的管理员权限，请稍后重试"


@dataclass(frozen=True, slots=True)
class BotCapabilities:
    is_admin: bool
    can_send: bool
    can_delete: bool

    @property
    def ready(self) -> bool:
        return self.is_admin and self.can_send and self.can_delete


class PermissionGateway(Protocol):
    async def resolve_channel(self, reference: str | int) -> Any: ...

    async def user_is_admin(self, channel_id: int, user_id: int) -> bool: ...

    async def bot_capabilities(self, channel_id: int) -> BotCapabilities: ...


class PermissionService:
    def __init__(self, repository: Repository, gateway: PermissionGateway) -> None:
        self.repository = repository
        self.gateway = gateway

    async def _chat_label(self, channel_id: int) -> str:
        channel = await self.repository.get_channel(channel_id)
        return "超级群组" if channel is not None and channel["chat_type"] == "supergroup" else "频道"

    async def assert_can_bind(self, user_id: int, reference: str | int):
        channel = await self.gateway.resolve_channel(reference)
        label = "超级群组" if getattr(channel, "chat_type", "channel") == "supergroup" else "频道"
        if not await self.gateway.user_is_admin(channel.id, user_id):
            raise PermissionDenied(f"你必须是该{label}的当前管理员")
        capabilities = await self.gateway.bot_capabilities(channel.id)
        if not capabilities.ready:
            raise PermissionDenied(f"机器人必须是{label}管理员，并拥有发送和删除消息权限")
        return channel

    async def assert_user_can_manage(self, user_id: int, channel_id: int) -> None:
        label = await self._chat_label(channel_id)
        if not await self.repository.is_bound_manager(user_id, channel_id):
            raise PermissionDenied(f"你尚未绑定此{label}")
        if not await self.gateway.user_is_admin(channel_id, user_id):
            await self.repository.unbind_manager(user_id, channel_id)
            raise PermissionDenied(f"你的{label}管理员权限已失效")
        capabilities = await self.gateway.bot_capabilities(channel_id)
        if not capabilities.ready:
            raise PermissionDenied(f"机器人缺少{label}发送或删除消息权限")
