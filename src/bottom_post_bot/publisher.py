from __future__ import annotations

from enum import StrEnum
from typing import Protocol, Sequence

from .domain import ButtonSpec, ContentItem, SlotSnapshot, enabled_slots_in_publish_order, group_content_items


class RefreshOutcome(StrEnum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    RETRY = "retry"
    PAUSED = "paused"


class FloodWaitSignal(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram requested a {seconds}-second flood wait")
        self.seconds = max(1, int(seconds))


class PermanentPublishError(Exception):
    """A publish error that requires an administrator to repair permissions or configuration."""


class TelegramGateway(Protocol):
    async def delete_messages(self, channel_id: int, message_ids: list[int]) -> None: ...

    async def send_content(
        self,
        channel_id: int,
        item: ContentItem,
        buttons: Sequence[ButtonSpec],
        silent: bool,
    ) -> list[int]: ...

    async def send_content_group(
        self,
        channel_id: int,
        items: Sequence[ContentItem],
        buttons: Sequence[ButtonSpec],
        silent: bool,
    ) -> list[int]: ...


class PublishState(Protocol):
    async def load_publish_state(self, channel_id: int) -> tuple[list[SlotSnapshot], list[int], bool]: ...

    async def commit_batch(self, channel_id: int, message_ids: list[int]) -> None: ...

    async def begin_batch(self, channel_id: int) -> int: ...

    async def record_batch_messages(self, batch_id: int, message_ids: list[int]) -> None: ...

    async def finalize_batch(self, channel_id: int, batch_id: int) -> None: ...

    async def fail_batch(self, batch_id: int, error: str, *, needs_cleanup: bool) -> None: ...

    async def mark_failure(self, channel_id: int, error: str) -> None: ...


class Publisher:
    def __init__(self, gateway: TelegramGateway, state: PublishState) -> None:
        self._gateway = gateway
        self._state = state

    async def refresh(self, channel_id: int) -> RefreshOutcome:
        slots, previous_ids, silent = await self._state.load_publish_state(channel_id)
        ordered = enabled_slots_in_publish_order(slots)
        if not ordered:
            if previous_ids:
                await self._gateway.delete_messages(channel_id, previous_ids)
                await self._state.commit_batch(channel_id, [])
            return RefreshOutcome.SKIPPED

        batch_id = await self._state.begin_batch(channel_id)
        sent_ids: list[int] = []
        try:
            if previous_ids:
                await self._gateway.delete_messages(channel_id, previous_ids)
            for slot in ordered:
                groups = group_content_items(slot.revision.items)
                for index, items in enumerate(groups):
                    buttons = slot.revision.buttons if index == len(groups) - 1 else ()
                    if len(items) > 1 and items[0].grouped_id:
                        new_ids = await self._gateway.send_content_group(channel_id, items, buttons, silent)
                    else:
                        new_ids = await self._gateway.send_content(channel_id, items[0], buttons, silent)
                    sent_ids.extend(new_ids)
                    await self._state.record_batch_messages(batch_id, new_ids)
        except Exception as exc:
            needs_cleanup = False
            if sent_ids:
                try:
                    await self._gateway.delete_messages(channel_id, sent_ids)
                except Exception:
                    needs_cleanup = True
            await self._state.fail_batch(batch_id, str(exc), needs_cleanup=needs_cleanup)
            await self._state.mark_failure(channel_id, str(exc))
            if isinstance(exc, FloodWaitSignal):
                raise
            if isinstance(exc, PermanentPublishError):
                return RefreshOutcome.PAUSED
            return RefreshOutcome.RETRY

        await self._state.finalize_batch(channel_id, batch_id)
        return RefreshOutcome.SUCCESS
