from __future__ import annotations

import logging
import time
from typing import Callable, Sequence

from .domain import ContentItem, Draft, PendingDraft
from .drafts import IncomingContent, StorageGateway, default_draft_name
from .repositories import AuthorizationError, Repository


logger = logging.getLogger(__name__)


class PendingDraftService:
    def __init__(
        self,
        repository: Repository,
        storage: StorageGateway,
        max_drafts: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.max_drafts = max_drafts
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    async def prepare(self, user_id: int, messages: Sequence[IncomingContent]) -> PendingDraft:
        if not messages:
            raise ValueError("at least one message is required")
        storage_ids = await self.storage.copy_messages(messages)
        try:
            if len(storage_ids) != len(messages):
                raise RuntimeError("storage gateway returned an unexpected number of messages")
            items = tuple(
                ContentItem(
                    text=message.text,
                    storage_message_id=storage_id,
                    media_kind=message.media_kind,
                    telegram_file_id=message.file_id,
                    grouped_id=message.grouped_id,
                    formatting_entities_json=message.formatting_entities_json,
                )
                for message, storage_id in zip(messages, storage_ids, strict=True)
            )
            return await self.repository.create_pending_draft(user_id, items, self.clock() + self.ttl_seconds)
        except Exception:
            try:
                await self.storage.delete_storage_messages(storage_ids)
            except Exception:
                logger.exception("Failed to compensate copied pending-draft messages")
            raise

    async def confirm(self, pending_id: int, user_id: int, name: str | None = None) -> Draft:
        pending = await self.assert_confirmable(pending_id, user_id)
        draft_name = (name or default_draft_name(pending.items)).strip()[:100] or "未命名草稿"
        return await self.repository.confirm_pending_draft(
            user_id, pending_id, draft_name, self.max_drafts, now=self.clock()
        )

    async def assert_confirmable(self, pending_id: int, user_id: int) -> PendingDraft:
        pending = await self.repository.get_pending_draft(user_id, pending_id)
        if pending is None or pending.status != "pending" or pending.expires_at <= self.clock():
            raise AuthorizationError("pending draft already processed or expired")
        return pending

    async def discard(self, pending_id: int, user_id: int) -> bool:
        pending = await self.repository.get_pending_draft(user_id, pending_id)
        if pending is None:
            return False
        if not await self.repository.mark_pending_discarded(user_id, pending_id, now=self.clock()):
            return False
        await self.storage.delete_storage_messages(
            [item.storage_message_id for item in pending.items if item.storage_message_id is not None]
        )
        await self.repository.complete_pending_cleanup(pending.id)
        return True

    async def cleanup_expired(self, now: float, limit: int = 100) -> int:
        cleaned = 0
        for pending in await self.repository.list_pending_cleanup(now, limit):
            try:
                await self.storage.delete_storage_messages(
                    [item.storage_message_id for item in pending.items if item.storage_message_id is not None]
                )
            except Exception:
                logger.exception("Failed to delete pending-draft storage messages", extra={"pending_draft_id": pending.id})
                continue
            if await self.repository.complete_pending_cleanup(pending.id):
                cleaned += 1
        return cleaned
