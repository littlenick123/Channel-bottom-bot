from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .domain import ButtonSpec, ContentItem, Draft, DraftRevision
from .repositories import AuthorizationError, Repository


@dataclass(frozen=True, slots=True)
class IncomingContent:
    source_chat_id: int
    source_message_id: int
    text: str | None
    media_kind: str | None
    grouped_id: str | None
    formatting_entities_json: str
    file_id: str | None = None


def default_draft_name(messages: Sequence[IncomingContent | ContentItem]) -> str:
    for message in messages:
        if message.text and message.text.strip():
            return " ".join(message.text.split())[:40]
    return "媒体草稿"


class StorageGateway(Protocol):
    async def copy_messages(self, messages: Sequence[IncomingContent]) -> list[int]: ...

    async def delete_storage_messages(self, message_ids: list[int]) -> None: ...


class DraftService:
    def __init__(self, repository: Repository, storage: StorageGateway, max_drafts: int) -> None:
        self.repository = repository
        self.storage = storage
        self.max_drafts = max_drafts

    async def capture(
        self, user_id: int, messages: Sequence[IncomingContent], name: str | None = None
    ) -> Draft:
        if not messages:
            raise ValueError("at least one message is required")
        storage_ids = await self.storage.copy_messages(messages)
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
        draft_name = (name or default_draft_name(messages)).strip()[:100] or "未命名草稿"
        return await self.repository.create_draft(user_id, draft_name, items, (), self.max_drafts)

    async def create_revision(
        self,
        draft_id: int,
        user_id: int,
        content: Sequence[ContentItem],
        buttons: Sequence[ButtonSpec],
    ) -> DraftRevision:
        return await self.repository.create_revision(draft_id, user_id, content, buttons)

    async def update_buttons(
        self, draft_id: int, user_id: int, buttons: Sequence[ButtonSpec]
    ) -> DraftRevision:
        draft = await self.repository.get_draft(user_id, draft_id)
        if not draft:
            raise AuthorizationError("draft not found or not owned by user")
        return await self.create_revision(draft_id, user_id, draft.current_revision.items, buttons)

    async def copy(self, draft_id: int, user_id: int, name: str | None = None) -> Draft:
        draft = await self.repository.get_draft(user_id, draft_id)
        if not draft:
            raise AuthorizationError("draft not found or not owned by user")
        return await self.repository.create_draft(
            user_id,
            (name or f"{draft.name} 副本")[:100],
            draft.current_revision.items,
            draft.current_revision.buttons,
            self.max_drafts,
        )

    async def garbage_collect(self, limit: int = 100) -> int:
        message_ids = await self.repository.list_collectable_storage_ids(limit)
        if not message_ids:
            return 0
        await self.storage.delete_storage_messages(message_ids)
        await self.repository.mark_storage_collected(message_ids)
        return len(message_ids)
