from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable
from urllib.parse import urlsplit


class ValidationError(ValueError):
    """Raised when user-provided domain data is invalid."""


def normalize_button_url(url: str) -> str:
    normalized_url = url.strip()
    lowered_url = normalized_url.lower()
    if not lowered_url.startswith(("https://", "http://", "tg://")):
        raise ValidationError("button URL must use https://, http:// or tg://")
    try:
        parsed_url = urlsplit(normalized_url)
    except ValueError as exc:
        raise ValidationError("button URL must use https://, http:// or tg://") from exc
    if lowered_url.startswith(("https://", "http://")) and not parsed_url.netloc:
        raise ValidationError("button URL must use https://, http:// or tg://")
    return normalized_url


class ContentKind(StrEnum):
    TEXT = "text"
    MEDIA = "media"


@dataclass(frozen=True, slots=True)
class ContentItem:
    text: str | None = None
    storage_message_id: int | None = None
    media_kind: str | None = None
    telegram_file_id: str | None = None
    grouped_id: str | None = None
    formatting_entities_json: str = "[]"

    def __post_init__(self) -> None:
        if not (self.text or self.storage_message_id or self.telegram_file_id):
            raise ValidationError("content item requires text or stored media")


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    text: str
    url: str
    row: int
    column: int

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValidationError("button text cannot be empty")
        if self.row < 0 or self.column < 0:
            raise ValidationError("button position cannot be negative")
        object.__setattr__(self, "url", normalize_button_url(self.url))


@dataclass(frozen=True, slots=True)
class DraftRevision:
    id: int
    revision_number: int
    items: tuple[ContentItem, ...]
    buttons: tuple[ButtonSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.items:
            raise ValidationError("draft revision cannot be empty")
        if len(self.buttons) > 100:
            raise ValidationError("a message can contain at most 100 buttons")
        row_counts: dict[int, int] = {}
        positions: set[tuple[int, int]] = set()
        for button in self.buttons:
            position = (button.row, button.column)
            if position in positions:
                raise ValidationError("button positions must be unique")
            positions.add(position)
            row_counts[button.row] = row_counts.get(button.row, 0) + 1
        if any(count > 8 for count in row_counts.values()):
            raise ValidationError("a button row can contain at most 8 buttons")


@dataclass(frozen=True, slots=True)
class Draft:
    id: int
    owner_user_id: int
    name: str
    current_revision: DraftRevision


@dataclass(frozen=True, slots=True)
class SlotSnapshot:
    channel_id: int
    slot_number: int
    revision: DraftRevision
    enabled: bool = True
    display_name: str = ""
    name_customized: bool = False

    def __post_init__(self) -> None:
        if self.slot_number <= 0:
            raise ValidationError("slot number must be positive")


@dataclass(frozen=True, slots=True)
class PendingDraft:
    id: int
    user_id: int
    items: tuple[ContentItem, ...]
    expires_at: float
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class RefreshJob:
    channel_id: int
    due_at: float
    generation: int
    attempts: int
    reason: str
    last_error: str | None = None


def enabled_slots_in_publish_order(slots: Iterable[SlotSnapshot]) -> list[SlotSnapshot]:
    return sorted((slot for slot in slots if slot.enabled and slot.revision.items), key=lambda slot: slot.slot_number, reverse=True)


def group_content_items(items: Iterable[ContentItem]) -> list[list[ContentItem]]:
    """Keep adjacent items from the same Telegram media group together."""
    groups: list[list[ContentItem]] = []
    for item in items:
        if item.grouped_id and groups and groups[-1][0].grouped_id == item.grouped_id:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups
