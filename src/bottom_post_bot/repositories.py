from __future__ import annotations

import json
import time
from datetime import date
from typing import Iterable, Sequence

from .database import Database
from .domain import (
    ButtonSpec,
    ContentItem,
    DailyMemberStats,
    DailyReportDelivery,
    Draft,
    DraftRevision,
    PendingDraft,
    RefreshJob,
    SlotSnapshot,
)


class ResourceLimitError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class Repository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def upsert_user(self, user_id: int, display_name: str) -> None:
        await self.db.execute(
            """INSERT INTO users(id, display_name) VALUES (?, ?)
               ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, updated_at=CURRENT_TIMESTAMP""",
            (user_id, display_name),
        )

    async def upsert_channel(
        self,
        channel_id: int,
        title: str,
        username: str | None,
        refresh_delay_seconds: int = 10,
        chat_type: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO channels(id, title, username, refresh_delay_seconds, chat_type) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username,
                   chat_type=COALESCE(?, chat_type), updated_at=CURRENT_TIMESTAMP""",
            (channel_id, title, username, refresh_delay_seconds, chat_type or "channel", chat_type),
        )

    async def bind_manager(self, user_id: int, channel_id: int, max_channels: int) -> bool:
        async with self.db.transaction() as connection:
            existing = await (
                await connection.execute(
                    "SELECT 1 FROM channel_managers WHERE user_id=? AND channel_id=?", (user_id, channel_id)
                )
            ).fetchone()
            if existing:
                return False
            count = await (
                await connection.execute("SELECT COUNT(*) FROM channel_managers WHERE user_id=?", (user_id,))
            ).fetchone()
            if int(count[0]) >= max_channels:
                raise ResourceLimitError(f"user can manage at most {max_channels} channels")
            await connection.execute(
                "INSERT INTO channel_managers(user_id, channel_id) VALUES (?, ?)", (user_id, channel_id)
            )
            return True

    async def unbind_manager(self, user_id: int, channel_id: int) -> None:
        async with self.db.transaction() as connection:
            count_row = await (
                await connection.execute("SELECT COUNT(*) FROM channel_managers WHERE channel_id=?", (channel_id,))
            ).fetchone()
            await connection.execute("DELETE FROM channel_managers WHERE user_id=? AND channel_id=?", (user_id, channel_id))
            if int(count_row[0]) == 1:
                await connection.execute(
                    """UPDATE chat_analytics_state
                       SET interruption_started_at=COALESCE(interruption_started_at, ?),
                           interruption_reason=COALESCE(interruption_reason, 'last manager unbound')
                       WHERE channel_id=?""",
                    (time.time(), channel_id),
                )

    async def is_bound_manager(self, user_id: int, channel_id: int) -> bool:
        return bool(
            await self.db.fetch_value(
                "SELECT 1 FROM channel_managers WHERE user_id=? AND channel_id=?", (user_id, channel_id)
            )
        )

    async def list_user_channels(self, user_id: int):
        return await self.db.fetch_all(
            """SELECT c.* FROM channels c JOIN channel_managers m ON m.channel_id=c.id
               WHERE m.user_id=? ORDER BY c.title""",
            (user_id,),
        )

    async def get_channel(self, channel_id: int):
        return await self.db.fetch_one("SELECT * FROM channels WHERE id=?", (channel_id,))

    async def has_channel(self, channel_id: int) -> bool:
        return bool(await self.db.fetch_value("SELECT 1 FROM channels WHERE id=?", (channel_id,)))

    async def has_channel_configuration(self, channel_id: int) -> bool:
        return bool(
            await self.db.fetch_value(
                """SELECT 1 FROM channels AS channel
                   WHERE channel.id=? AND (
                       EXISTS (SELECT 1 FROM channel_managers AS manager WHERE manager.channel_id=channel.id)
                       OR EXISTS (SELECT 1 FROM channel_slots AS slot WHERE slot.channel_id=channel.id)
                       OR EXISTS (SELECT 1 FROM sent_batches AS batch WHERE batch.channel_id=channel.id)
                       OR EXISTS (SELECT 1 FROM refresh_jobs AS job WHERE job.channel_id=channel.id)
                   )""",
                (channel_id,),
            )
        )

    async def list_channel_slots(self, channel_id: int) -> list[SlotSnapshot]:
        rows = await self.db.fetch_all(
            """SELECT slot_number, revision_id, enabled, display_name, name_customized
               FROM channel_slots WHERE channel_id=? ORDER BY slot_number""",
            (channel_id,),
        )
        return [
            SlotSnapshot(
                channel_id,
                int(row["slot_number"]),
                await self.load_revision(int(row["revision_id"])),
                bool(row["enabled"]),
                str(row["display_name"]),
                bool(row["name_customized"]),
            )
            for row in rows
        ]

    async def channel_refresh_delay(self, channel_id: int) -> int | None:
        row = await self.db.fetch_one(
            "SELECT refresh_delay_seconds FROM channels WHERE id=? AND enabled=1 AND status!='paused'", (channel_id,)
        )
        return None if not row else int(row["refresh_delay_seconds"])

    async def is_current_sent_message(self, channel_id: int, message_id: int) -> bool:
        return bool(
            await self.db.fetch_value(
                """SELECT 1 FROM sent_messages m JOIN sent_batches b ON b.id=m.batch_id
                   WHERE b.channel_id=? AND (b.is_current=1 OR b.status='sending') AND m.message_id=?""",
                (channel_id, message_id),
            )
        )

    async def create_draft(
        self,
        owner_user_id: int,
        name: str,
        items: Sequence[ContentItem],
        buttons: Sequence[ButtonSpec],
        max_drafts: int,
    ) -> Draft:
        async with self.db.transaction() as connection:
            count = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM drafts WHERE owner_user_id=? AND deleted_at IS NULL", (owner_user_id,)
                )
            ).fetchone()
            if int(count[0]) >= max_drafts:
                raise ResourceLimitError(f"user can store at most {max_drafts} drafts")
            cursor = await connection.execute(
                "INSERT INTO drafts(owner_user_id, name) VALUES (?, ?)", (owner_user_id, name.strip() or "未命名草稿")
            )
            draft_id = int(cursor.lastrowid)
            revision = await self._insert_revision(connection, draft_id, 1, items, buttons)
            await connection.execute("UPDATE drafts SET current_revision_id=? WHERE id=?", (revision.id, draft_id))
        return Draft(draft_id, owner_user_id, name.strip() or "未命名草稿", revision)

    async def create_pending_draft(
        self, user_id: int, items: Sequence[ContentItem], expires_at: float
    ) -> PendingDraft:
        pending_items = tuple(items)
        if not pending_items:
            raise ValueError("pending draft cannot be empty")
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "INSERT INTO pending_drafts(user_id, expires_at) VALUES (?, ?)", (user_id, expires_at)
            )
            pending_id = int(cursor.lastrowid)
            await connection.executemany(
                """INSERT INTO pending_draft_items(
                       pending_draft_id, position, text, storage_message_id, media_kind, telegram_file_id,
                       grouped_id, formatting_entities_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        pending_id,
                        position,
                        item.text,
                        item.storage_message_id,
                        item.media_kind,
                        item.telegram_file_id,
                        item.grouped_id,
                        item.formatting_entities_json,
                    )
                    for position, item in enumerate(pending_items)
                ],
            )
        return PendingDraft(pending_id, user_id, pending_items, expires_at)

    async def get_pending_draft(self, user_id: int, pending_id: int) -> PendingDraft | None:
        row = await self.db.fetch_one(
            "SELECT id, user_id, status, expires_at FROM pending_drafts WHERE id=? AND user_id=?",
            (pending_id, user_id),
        )
        if not row:
            return None
        items = await self._load_pending_items(pending_id)
        return PendingDraft(int(row["id"]), int(row["user_id"]), items, float(row["expires_at"]), str(row["status"]))

    async def confirm_pending_draft(
        self,
        user_id: int,
        pending_id: int,
        name: str,
        max_drafts: int,
        *,
        now: float | None = None,
    ) -> Draft:
        confirmed_at = time.time() if now is None else now
        async with self.db.transaction() as connection:
            row = await (
                await connection.execute(
                    """SELECT id, expires_at FROM pending_drafts
                       WHERE id=? AND user_id=? AND status='pending' AND expires_at>?""",
                    (pending_id, user_id, confirmed_at),
                )
            ).fetchone()
            if not row:
                raise AuthorizationError("pending draft already processed or expired")
            count = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM drafts WHERE owner_user_id=? AND deleted_at IS NULL", (user_id,)
                )
            ).fetchone()
            if int(count[0]) >= max_drafts:
                raise ResourceLimitError(f"user can store at most {max_drafts} drafts")
            items = await self._load_pending_items(pending_id, connection)
            draft_name = name.strip()[:100] or "未命名草稿"
            cursor = await connection.execute("INSERT INTO drafts(owner_user_id, name) VALUES (?, ?)", (user_id, draft_name))
            draft_id = int(cursor.lastrowid)
            revision = await self._insert_revision(connection, draft_id, 1, items, ())
            await connection.execute("UPDATE drafts SET current_revision_id=? WHERE id=?", (revision.id, draft_id))
            await connection.execute("DELETE FROM pending_drafts WHERE id=?", (pending_id,))
        return Draft(draft_id, user_id, draft_name, revision)

    async def mark_pending_discarded(self, user_id: int, pending_id: int, *, now: float | None = None) -> bool:
        discarded_at = time.time() if now is None else now
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE pending_drafts SET status='discarded', updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND user_id=? AND status='pending' AND expires_at>?""",
                (pending_id, user_id, discarded_at),
            )
            return cursor.rowcount > 0

    async def list_pending_cleanup(self, now: float, limit: int = 100) -> list[PendingDraft]:
        async with self.db.transaction() as connection:
            await connection.execute(
                """UPDATE pending_drafts SET status='expired', updated_at=CURRENT_TIMESTAMP
                   WHERE status='pending' AND expires_at<=?""",
                (now,),
            )
            rows = await (
                await connection.execute(
                    """SELECT id, user_id, status, expires_at FROM pending_drafts
                       WHERE status IN ('discarded', 'expired') ORDER BY id LIMIT ?""",
                    (limit,),
                )
            ).fetchall()
            result: list[PendingDraft] = []
            for row in rows:
                items = await self._load_pending_items(int(row["id"]), connection)
                result.append(
                    PendingDraft(
                        int(row["id"]),
                        int(row["user_id"]),
                        items,
                        float(row["expires_at"]),
                        str(row["status"]),
                    )
                )
            return result

    async def complete_pending_cleanup(self, pending_id: int) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM pending_drafts WHERE id=? AND status IN ('discarded', 'expired')", (pending_id,)
            )
            return cursor.rowcount > 0

    async def create_revision(
        self,
        draft_id: int,
        owner_user_id: int,
        items: Sequence[ContentItem],
        buttons: Sequence[ButtonSpec],
    ) -> DraftRevision:
        async with self.db.transaction() as connection:
            draft = await (
                await connection.execute(
                    "SELECT id FROM drafts WHERE id=? AND owner_user_id=? AND deleted_at IS NULL",
                    (draft_id, owner_user_id),
                )
            ).fetchone()
            if not draft:
                raise AuthorizationError("draft not found or not owned by user")
            row = await (
                await connection.execute(
                    "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM draft_revisions WHERE draft_id=?", (draft_id,)
                )
            ).fetchone()
            revision = await self._insert_revision(connection, draft_id, int(row[0]), items, buttons)
            await connection.execute(
                "UPDATE drafts SET current_revision_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (revision.id, draft_id),
            )
            return revision

    async def _insert_revision(
        self, connection, draft_id: int, number: int, items: Sequence[ContentItem], buttons: Sequence[ButtonSpec]
    ) -> DraftRevision:
        revision_value = DraftRevision(0, number, tuple(items), tuple(buttons))
        cursor = await connection.execute(
            "INSERT INTO draft_revisions(draft_id, revision_number) VALUES (?, ?)", (draft_id, number)
        )
        revision_id = int(cursor.lastrowid)
        await connection.executemany(
            """INSERT INTO content_items(
                   revision_id, position, text, storage_message_id, media_kind, telegram_file_id,
                   grouped_id, formatting_entities_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    revision_id,
                    position,
                    item.text,
                    item.storage_message_id,
                    item.media_kind,
                    item.telegram_file_id,
                    item.grouped_id,
                    item.formatting_entities_json,
                )
                for position, item in enumerate(revision_value.items)
            ],
        )
        await connection.executemany(
            """INSERT INTO draft_buttons(revision_id, row_number, column_number, text, url)
               VALUES (?, ?, ?, ?, ?)""",
            [(revision_id, item.row, item.column, item.text, item.url) for item in revision_value.buttons],
        )
        return DraftRevision(revision_id, number, revision_value.items, revision_value.buttons)

    async def get_draft(self, owner_user_id: int, draft_id: int) -> Draft | None:
        row = await self.db.fetch_one(
            """SELECT id, owner_user_id, name, current_revision_id FROM drafts
               WHERE id=? AND owner_user_id=? AND deleted_at IS NULL""",
            (draft_id, owner_user_id),
        )
        if not row:
            return None
        revision = await self.load_revision(int(row["current_revision_id"]))
        return Draft(int(row["id"]), int(row["owner_user_id"]), str(row["name"]), revision)

    async def list_drafts(self, owner_user_id: int) -> list[Draft]:
        rows = await self.db.fetch_all(
            "SELECT id FROM drafts WHERE owner_user_id=? AND deleted_at IS NULL ORDER BY updated_at DESC", (owner_user_id,)
        )
        result: list[Draft] = []
        for row in rows:
            draft = await self.get_draft(owner_user_id, int(row["id"]))
            if draft:
                result.append(draft)
        return result

    async def rename_draft(self, owner_user_id: int, draft_id: int, name: str) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE drafts SET name=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=? AND deleted_at IS NULL""",
                (name.strip() or "未命名草稿", draft_id, owner_user_id),
            )
            return cursor.rowcount > 0

    async def delete_draft(self, owner_user_id: int, draft_id: int) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE drafts SET deleted_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=? AND deleted_at IS NULL",
                (draft_id, owner_user_id),
            )
            return cursor.rowcount > 0

    async def set_conversation(self, user_id: int, state: str, payload: dict, expires_at: float) -> None:
        await self.db.execute(
            """INSERT INTO conversation_states(user_id, state, payload_json, expires_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, payload_json=excluded.payload_json,
                   expires_at=excluded.expires_at""",
            (user_id, state, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), expires_at),
        )

    async def get_conversation(self, user_id: int) -> tuple[str, dict] | None:
        row = await self.db.fetch_one("SELECT state, payload_json, expires_at FROM conversation_states WHERE user_id=?", (user_id,))
        if not row:
            return None
        if float(row["expires_at"]) < time.time():
            await self.clear_conversation(user_id)
            return None
        return str(row["state"]), json.loads(row["payload_json"])

    async def clear_conversation(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM conversation_states WHERE user_id=?", (user_id,))

    async def load_revision(self, revision_id: int) -> DraftRevision:
        row = await self.db.fetch_one(
            "SELECT id, revision_number FROM draft_revisions WHERE id=?", (revision_id,)
        )
        if not row:
            raise LookupError(f"revision {revision_id} not found")
        item_rows = await self.db.fetch_all(
            "SELECT * FROM content_items WHERE revision_id=? ORDER BY position", (revision_id,)
        )
        button_rows = await self.db.fetch_all(
            "SELECT * FROM draft_buttons WHERE revision_id=? ORDER BY row_number, column_number", (revision_id,)
        )
        items = tuple(
            ContentItem(
                text=item["text"],
                storage_message_id=item["storage_message_id"],
                media_kind=item["media_kind"],
                telegram_file_id=item["telegram_file_id"],
                grouped_id=item["grouped_id"],
                formatting_entities_json=item["formatting_entities_json"],
            )
            for item in item_rows
        )
        buttons = tuple(
            ButtonSpec(item["text"], item["url"], int(item["row_number"]), int(item["column_number"]))
            for item in button_rows
        )
        return DraftRevision(int(row["id"]), int(row["revision_number"]), items, buttons)

    async def _load_pending_items(self, pending_id: int, connection=None) -> tuple[ContentItem, ...]:
        if connection is None:
            rows = await self.db.fetch_all(
                "SELECT * FROM pending_draft_items WHERE pending_draft_id=? ORDER BY position", (pending_id,)
            )
        else:
            rows = await (
                await connection.execute(
                    "SELECT * FROM pending_draft_items WHERE pending_draft_id=? ORDER BY position", (pending_id,)
                )
            ).fetchall()
        return tuple(
            ContentItem(
                text=row["text"],
                storage_message_id=row["storage_message_id"],
                media_kind=row["media_kind"],
                telegram_file_id=row["telegram_file_id"],
                grouped_id=row["grouped_id"],
                formatting_entities_json=row["formatting_entities_json"],
            )
            for row in rows
        )

    async def revision_is_owned_by(self, revision_id: int, user_id: int) -> bool:
        return bool(
            await self.db.fetch_value(
                """SELECT 1 FROM draft_revisions r JOIN drafts d ON d.id=r.draft_id
                   WHERE r.id=? AND d.owner_user_id=? AND d.deleted_at IS NULL""",
                (revision_id, user_id),
            )
        )

    async def owned_draft_name_for_revision(self, revision_id: int, user_id: int) -> str | None:
        row = await self.db.fetch_one(
            """SELECT d.name FROM draft_revisions r JOIN drafts d ON d.id=r.draft_id
               WHERE r.id=? AND d.owner_user_id=? AND d.deleted_at IS NULL""",
            (revision_id, user_id),
        )
        return None if not row else str(row["name"])

    async def assign_slot(
        self,
        channel_id: int,
        slot_number: int,
        revision_id: int,
        actor_id: int,
        max_slots: int,
        display_name: str = "",
    ) -> None:
        if slot_number < 1 or slot_number > max_slots:
            raise ResourceLimitError(f"slot number must be between 1 and {max_slots}")
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        await self.load_revision(revision_id)
        await self.db.execute(
            """INSERT INTO channel_slots(channel_id, slot_number, revision_id, display_name, updated_by)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(channel_id, slot_number) DO UPDATE SET
                   revision_id=excluded.revision_id,
                   display_name=CASE WHEN channel_slots.name_customized=1
                                     THEN channel_slots.display_name ELSE excluded.display_name END,
                   enabled=1,
                   updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP""",
            (channel_id, slot_number, revision_id, display_name, actor_id),
        )
        await self.audit(channel_id, actor_id, "slot.assign", {"slot": slot_number})

    async def rename_slot(self, channel_id: int, slot_number: int, display_name: str, actor_id: int) -> None:
        name = display_name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("槽位名称长度必须为 1 到 100 个字符")
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        if not await self.db.fetch_value(
            "SELECT 1 FROM channel_slots WHERE channel_id=? AND slot_number=?", (channel_id, slot_number)
        ):
            raise LookupError("slot is empty")
        await self.db.execute(
            """UPDATE channel_slots SET display_name=?, name_customized=1,
               updated_by=?, updated_at=CURRENT_TIMESTAMP WHERE channel_id=? AND slot_number=?""",
            (name, actor_id, channel_id, slot_number),
        )
        await self.audit(channel_id, actor_id, "slot.rename", {"slot": slot_number, "name": name})

    async def set_slot_enabled(self, channel_id: int, slot_number: int, enabled: bool, actor_id: int) -> None:
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        await self.db.execute(
            "UPDATE channel_slots SET enabled=?, updated_by=?, updated_at=CURRENT_TIMESTAMP WHERE channel_id=? AND slot_number=?",
            (int(enabled), actor_id, channel_id, slot_number),
        )

    async def clear_slot(self, channel_id: int, slot_number: int, actor_id: int) -> None:
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        await self.db.execute("DELETE FROM channel_slots WHERE channel_id=? AND slot_number=?", (channel_id, slot_number))
        await self.audit(channel_id, actor_id, "slot.clear", {"slot": slot_number})

    async def move_slot(
        self,
        channel_id: int,
        source_number: int,
        target_number: int,
        actor_id: int,
        max_slots: int,
    ) -> None:
        if not 1 <= source_number <= max_slots or not 1 <= target_number <= max_slots:
            raise ResourceLimitError(f"slot number must be between 1 and {max_slots}")
        if source_number == target_number:
            return
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        async with self.db.transaction() as connection:
            source = await (
                await connection.execute(
                    "SELECT 1 FROM channel_slots WHERE channel_id=? AND slot_number=?",
                    (channel_id, source_number),
                )
            ).fetchone()
            if not source:
                raise LookupError("source slot is empty")
            target = await (
                await connection.execute(
                    "SELECT 1 FROM channel_slots WHERE channel_id=? AND slot_number=?",
                    (channel_id, target_number),
                )
            ).fetchone()
            if target:
                await connection.execute(
                    "UPDATE channel_slots SET slot_number=0 WHERE channel_id=? AND slot_number=?",
                    (channel_id, source_number),
                )
                await connection.execute(
                    """UPDATE channel_slots SET slot_number=?, updated_by=?, updated_at=CURRENT_TIMESTAMP
                       WHERE channel_id=? AND slot_number=?""",
                    (source_number, actor_id, channel_id, target_number),
                )
                await connection.execute(
                    """UPDATE channel_slots SET slot_number=?, updated_by=?, updated_at=CURRENT_TIMESTAMP
                       WHERE channel_id=? AND slot_number=0""",
                    (target_number, actor_id, channel_id),
                )
            else:
                await connection.execute(
                    """UPDATE channel_slots SET slot_number=?, updated_by=?, updated_at=CURRENT_TIMESTAMP
                       WHERE channel_id=? AND slot_number=?""",
                    (target_number, actor_id, channel_id, source_number),
                )
        await self.audit(channel_id, actor_id, "slot.move", {"from": source_number, "to": target_number})

    async def set_channel_options(
        self,
        channel_id: int,
        actor_id: int,
        *,
        enabled: bool | None = None,
        silent: bool | None = None,
        refresh_delay_seconds: int | None = None,
    ) -> None:
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        assignments: list[str] = []
        values: list[object] = []
        if enabled is not None:
            assignments.append("enabled=?")
            values.append(int(enabled))
        if silent is not None:
            assignments.append("silent=?")
            values.append(int(silent))
        if refresh_delay_seconds is not None:
            if not 1 <= refresh_delay_seconds <= 3600:
                raise ValueError("refresh delay must be between 1 and 3600 seconds")
            assignments.append("refresh_delay_seconds=?")
            values.append(refresh_delay_seconds)
        if not assignments:
            return
        assignments.append("updated_at=CURRENT_TIMESTAMP")
        values.append(channel_id)
        await self.db.execute(f"UPDATE channels SET {', '.join(assignments)} WHERE id=?", values)
        await self.audit(channel_id, actor_id, "channel.options", {})

    async def load_publish_state(self, channel_id: int) -> tuple[list[SlotSnapshot], list[int], bool]:
        channel = await self.db.fetch_one("SELECT enabled, silent FROM channels WHERE id=?", (channel_id,))
        message_rows = await self.db.fetch_all(
            """SELECT message_id, position FROM (
                   SELECT m.message_id AS message_id, m.position AS position
                   FROM sent_messages m JOIN sent_batches b ON b.id=m.batch_id
                   WHERE b.channel_id=? AND b.is_current=1
                   UNION
                   SELECT o.message_id AS message_id, 1000000 AS position
                   FROM orphan_messages o WHERE o.channel_id=?
               ) ORDER BY position""",
            (channel_id, channel_id),
        )
        previous = [int(row["message_id"]) for row in message_rows]
        if not channel or not bool(channel["enabled"]):
            return [], previous, True
        rows = await self.db.fetch_all(
            """SELECT slot_number, revision_id, enabled, display_name, name_customized
               FROM channel_slots WHERE channel_id=?""",
            (channel_id,),
        )
        slots = [
            SlotSnapshot(
                channel_id,
                int(row["slot_number"]),
                await self.load_revision(int(row["revision_id"])),
                bool(row["enabled"]),
                str(row["display_name"]),
                bool(row["name_customized"]),
            )
            for row in rows
        ]
        return slots, previous, bool(channel["silent"])

    async def begin_batch(self, channel_id: int) -> int:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "INSERT INTO sent_batches(channel_id, status, is_current) VALUES (?, 'sending', 0)", (channel_id,)
            )
            return int(cursor.lastrowid)

    async def record_batch_messages(self, batch_id: int, message_ids: list[int]) -> None:
        async with self.db.transaction() as connection:
            row = await (
                await connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM sent_messages WHERE batch_id=?", (batch_id,))
            ).fetchone()
            start = int(row[0])
            await connection.executemany(
                "INSERT OR IGNORE INTO sent_messages(batch_id, message_id, position) VALUES (?, ?, ?)",
                [(batch_id, message_id, start + offset) for offset, message_id in enumerate(message_ids)],
            )

    async def fail_batch(self, batch_id: int, error: str, *, needs_cleanup: bool) -> None:
        async with self.db.transaction() as connection:
            batch = await (
                await connection.execute("SELECT channel_id FROM sent_batches WHERE id=?", (batch_id,))
            ).fetchone()
            if not batch:
                return
            if needs_cleanup:
                await connection.execute(
                    """INSERT OR IGNORE INTO orphan_messages(channel_id, message_id)
                       SELECT channel_id, message_id FROM sent_batches b JOIN sent_messages m ON m.batch_id=b.id
                       WHERE b.id=?""",
                    (batch_id,),
                )
            await connection.execute(
                "UPDATE sent_batches SET status='failed', error=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (error[:1000], batch_id),
            )

    async def finalize_batch(self, channel_id: int, batch_id: int) -> None:
        async with self.db.transaction() as connection:
            await connection.execute("UPDATE sent_batches SET is_current=0 WHERE channel_id=?", (channel_id,))
            await connection.execute(
                """UPDATE sent_batches SET status='complete', is_current=1, completed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND channel_id=?""",
                (batch_id, channel_id),
            )
            await connection.execute("DELETE FROM orphan_messages WHERE channel_id=?", (channel_id,))
            await connection.execute(
                """UPDATE channels SET status='active', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status!='paused'""",
                (channel_id,),
            )

    async def recover_incomplete_batches(self, now: float) -> int:
        async with self.db.transaction() as connection:
            rows = await (
                await connection.execute("SELECT id, channel_id FROM sent_batches WHERE status='sending'")
            ).fetchall()
            for row in rows:
                await connection.execute(
                    """INSERT OR IGNORE INTO orphan_messages(channel_id, message_id)
                       SELECT ?, message_id FROM sent_messages WHERE batch_id=?""",
                    (row["channel_id"], row["id"]),
                )
                await connection.execute(
                    "UPDATE sent_batches SET status='interrupted', error='process interrupted', completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],),
                )
                await connection.execute(
                    """INSERT INTO refresh_jobs(channel_id, due_at, generation, attempts, reason)
                       VALUES (?, ?, 1, 0, 'startup-recovery')
                       ON CONFLICT(channel_id) DO UPDATE SET due_at=excluded.due_at,
                           generation=refresh_jobs.generation+1, attempts=0, reason=excluded.reason""",
                    (row["channel_id"], now),
                )
            return len(rows)

    async def commit_batch(self, channel_id: int, message_ids: list[int]) -> None:
        async with self.db.transaction() as connection:
            await connection.execute("UPDATE sent_batches SET is_current=0 WHERE channel_id=?", (channel_id,))
            cursor = await connection.execute(
                """INSERT INTO sent_batches(channel_id, status, is_current, completed_at)
                   VALUES (?, 'complete', 1, CURRENT_TIMESTAMP)""",
                (channel_id,),
            )
            batch_id = int(cursor.lastrowid)
            await connection.executemany(
                "INSERT INTO sent_messages(batch_id, message_id, position) VALUES (?, ?, ?)",
                [(batch_id, message_id, position) for position, message_id in enumerate(message_ids)],
            )
            await connection.execute("DELETE FROM orphan_messages WHERE channel_id=?", (channel_id,))
            await connection.execute(
                "UPDATE channels SET status='active', last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (channel_id,),
            )

    async def mark_failure(self, channel_id: int, error: str) -> None:
        await self.db.execute(
            "UPDATE channels SET status='error', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (error[:1000], channel_id),
        )

    async def audit(self, channel_id: int | None, actor_id: int | None, action: str, details: dict) -> None:
        await self.db.execute(
            "INSERT INTO audit_logs(channel_id, actor_user_id, action, detail_json) VALUES (?, ?, ?, ?)",
            (channel_id, actor_id, action, json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
        )

    async def schedule_refresh(self, channel_id: int, due_at: float, reason: str) -> RefreshJob:
        await self.db.execute(
            """INSERT INTO refresh_jobs(channel_id, due_at, generation, attempts, reason)
               VALUES (?, ?, 1, 0, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   due_at=excluded.due_at,
                   generation=refresh_jobs.generation + 1,
                   attempts=0,
                   reason=excluded.reason,
                   last_error=NULL""",
            (channel_id, due_at, reason),
        )
        job = await self.get_refresh_job(channel_id)
        assert job is not None
        return job

    async def get_refresh_job(self, channel_id: int) -> RefreshJob | None:
        row = await self.db.fetch_one("SELECT * FROM refresh_jobs WHERE channel_id=?", (channel_id,))
        return self._job_from_row(row) if row else None

    async def list_due_refresh_jobs(self, now: float) -> list[RefreshJob]:
        rows = await self.db.fetch_all("SELECT * FROM refresh_jobs WHERE due_at<=? ORDER BY due_at", (now,))
        return [self._job_from_row(row) for row in rows]

    async def next_refresh_due_at(self) -> float | None:
        value = await self.db.fetch_value("SELECT MIN(due_at) FROM refresh_jobs")
        return None if value is None else float(value)

    async def complete_refresh(self, channel_id: int, generation: int) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM refresh_jobs WHERE channel_id=? AND generation=?", (channel_id, generation)
            )
            return cursor.rowcount > 0

    async def retry_refresh(
        self,
        channel_id: int,
        generation: int,
        due_at: float,
        error: str,
        *,
        increment_attempts: bool,
    ) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE refresh_jobs SET due_at=?, attempts=attempts+?, last_error=?
                   WHERE channel_id=? AND generation=?""",
                (due_at, int(increment_attempts), error[:1000], channel_id, generation),
            )
            return cursor.rowcount > 0

    async def pause_channel(self, channel_id: int, error: str) -> None:
        async with self.db.transaction() as connection:
            await connection.execute(
                "UPDATE channels SET status='paused', enabled=0, last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (error[:1000], channel_id),
            )
            await connection.execute("DELETE FROM refresh_jobs WHERE channel_id=?", (channel_id,))

    async def resume_channel(self, channel_id: int, actor_id: int) -> None:
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        await self.db.execute(
            "UPDATE channels SET enabled=1, status='active', last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (channel_id,),
        )
        await self.audit(channel_id, actor_id, "channel.resume", {})

    async def delete_channel_config(self, channel_id: int, actor_id: int) -> None:
        if not await self.is_bound_manager(actor_id, channel_id):
            raise AuthorizationError("user has not bound this channel")
        await self.audit(channel_id, actor_id, "channel.delete", {})
        await self.db.execute("DELETE FROM channels WHERE id=?", (channel_id,))

    async def list_manager_ids(self, channel_id: int) -> list[int]:
        rows = await self.db.fetch_all("SELECT user_id FROM channel_managers WHERE channel_id=?", (channel_id,))
        return [int(row["user_id"]) for row in rows]

    async def is_stats_managed_channel(self, channel_id: int) -> bool:
        return bool(
            await self.db.fetch_value(
                """SELECT 1 FROM channels AS channel
                   WHERE channel.id=? AND EXISTS (
                       SELECT 1 FROM channel_managers AS manager WHERE manager.channel_id=channel.id
                   )""",
                (channel_id,),
            )
        )

    async def list_managed_channels(self):
        return await self.db.fetch_all(
            """SELECT c.* FROM channels AS c
               WHERE EXISTS (SELECT 1 FROM channel_managers AS m WHERE m.channel_id=c.id)
               ORDER BY c.id"""
        )

    async def get_manager_stats_push_enabled(self, user_id: int, channel_id: int) -> bool | None:
        row = await self.db.fetch_one(
            "SELECT stats_push_enabled FROM channel_managers WHERE user_id=? AND channel_id=?", (user_id, channel_id)
        )
        return None if row is None else bool(row["stats_push_enabled"])

    async def set_manager_stats_push_enabled(self, user_id: int, channel_id: int, enabled: bool) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE channel_managers SET stats_push_enabled=? WHERE user_id=? AND channel_id=?",
                (int(enabled), user_id, channel_id),
            )
            return cursor.rowcount > 0

    async def list_daily_report_manager_ids(self, cutoff_utc: str) -> list[int]:
        """Users with at least one opted-in chat bound before the local report cutoff."""
        rows = await self.db.fetch_all(
            """SELECT DISTINCT user_id FROM channel_managers
               WHERE stats_push_enabled=1 AND bound_at <= ? ORDER BY user_id""",
            (cutoff_utc,),
        )
        return [int(row["user_id"]) for row in rows]

    async def list_user_stats_subscription_ids(self, user_id: int, cutoff_utc: str) -> list[int]:
        rows = await self.db.fetch_all(
            """SELECT channel_id FROM channel_managers
               WHERE user_id=? AND stats_push_enabled=1 AND bound_at <= ?
               ORDER BY channel_id""",
            (user_id, cutoff_utc),
        )
        return [int(row["channel_id"]) for row in rows]

    async def initialize_analytics(self, channel_id: int, started_at: float, stat_date: str) -> bool:
        """Create collection state once and flag the partial activation day."""
        async with self.db.transaction() as connection:
            exists = await (
                await connection.execute(
                    """SELECT 1 FROM channel_managers WHERE channel_id=? LIMIT 1""", (channel_id,)
                )
            ).fetchone()
            if not exists:
                return False
            cursor = await connection.execute(
                "INSERT OR IGNORE INTO chat_analytics_state(channel_id, started_at) VALUES (?, ?)",
                (channel_id, started_at),
            )
            if cursor.rowcount:
                await self._mark_member_dates_incomplete(
                    connection, channel_id, (stat_date,), "statistics started during this day"
                )
            return True

    async def bind_manager_with_analytics(
        self,
        user_id: int,
        display_name: str,
        channel_id: int,
        title: str,
        username: str | None,
        refresh_delay_seconds: int,
        chat_type: str,
        max_channels: int,
        started_at: float,
        stat_date: str,
    ) -> tuple[bool, bool]:
        """Atomically make a chat manageable and establish its analytics boundary."""
        async with self.db.transaction() as connection:
            await connection.execute(
                "INSERT INTO users(id, display_name) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (user_id, display_name),
            )
            await connection.execute(
                """INSERT INTO channels(id, title, username, refresh_delay_seconds, chat_type) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username,
                       chat_type=COALESCE(?, chat_type), updated_at=CURRENT_TIMESTAMP""",
                (channel_id, title, username, refresh_delay_seconds, chat_type, chat_type),
            )
            existing = await (
                await connection.execute(
                    "SELECT 1 FROM channel_managers WHERE user_id=? AND channel_id=?", (user_id, channel_id)
                )
            ).fetchone()
            if existing:
                return False, False
            count = await (
                await connection.execute("SELECT COUNT(*) FROM channel_managers WHERE user_id=?", (user_id,))
            ).fetchone()
            if int(count[0]) >= max_channels:
                raise ResourceLimitError(f"user can manage at most {max_channels} channels")
            await connection.execute(
                "INSERT INTO channel_managers(user_id, channel_id) VALUES (?, ?)", (user_id, channel_id)
            )
            created_state = await connection.execute(
                "INSERT OR IGNORE INTO chat_analytics_state(channel_id, started_at) VALUES (?, ?)",
                (channel_id, started_at),
            )
            if created_state.rowcount:
                await self._mark_member_dates_incomplete(
                    connection, channel_id, (stat_date,), "statistics started during this day"
                )
            return True, bool(created_state.rowcount)

    async def record_member_transition(
        self,
        update_id: int,
        channel_id: int,
        direction: str,
        event_at: float,
        stat_date: str,
    ) -> bool:
        if direction not in {"join", "leave"}:
            raise ValueError("direction must be join or leave")
        async with self.db.transaction() as connection:
            managed = await (
                await connection.execute("SELECT 1 FROM channel_managers WHERE channel_id=? LIMIT 1", (channel_id,))
            ).fetchone()
            if not managed:
                return False
            created_state = await connection.execute(
                "INSERT OR IGNORE INTO chat_analytics_state(channel_id, started_at) VALUES (?, ?)",
                (channel_id, event_at),
            )
            if created_state.rowcount:
                await self._mark_member_dates_incomplete(
                    connection, channel_id, (stat_date,), "statistics started during this day"
                )
            state = await (
                await connection.execute("SELECT started_at FROM chat_analytics_state WHERE channel_id=?", (channel_id,))
            ).fetchone()
            ignored = event_at < float(state["started_at"])
            inserted = await connection.execute(
                """INSERT OR IGNORE INTO processed_member_updates(update_id, channel_id, direction, event_at, ignored)
                   VALUES (?, ?, ?, ?, ?)""",
                (update_id, channel_id, direction, event_at, int(ignored)),
            )
            if not inserted.rowcount or ignored:
                return False
            column = "joined_count" if direction == "join" else "left_count"
            await connection.execute(
                f"""INSERT INTO member_daily_stats(channel_id, stat_date, {column}) VALUES (?, ?, 1)
                    ON CONFLICT(channel_id, stat_date) DO UPDATE SET
                        {column}={column}+1, updated_at=CURRENT_TIMESTAMP""",
                (channel_id, stat_date),
            )
            return True

    async def begin_analytics_interruption(self, channel_id: int, started_at: float, reason: str) -> bool:
        """Persist the first unavailable instant; later failures must not shorten the gap."""
        async with self.db.transaction() as connection:
            managed = await (
                await connection.execute("SELECT 1 FROM channel_managers WHERE channel_id=? LIMIT 1", (channel_id,))
            ).fetchone()
            if not managed:
                return False
            await connection.execute(
                "INSERT OR IGNORE INTO chat_analytics_state(channel_id, started_at) VALUES (?, ?)",
                (channel_id, started_at),
            )
            cursor = await connection.execute(
                """UPDATE chat_analytics_state
                   SET interruption_started_at=COALESCE(interruption_started_at, ?),
                       interruption_reason=COALESCE(interruption_reason, ?)
                   WHERE channel_id=?""",
                (started_at, reason[:400], channel_id),
            )
            return cursor.rowcount > 0

    async def end_analytics_interruption(self, channel_id: int) -> tuple[float, str] | None:
        async with self.db.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT interruption_started_at, interruption_reason FROM chat_analytics_state WHERE channel_id=?",
                    (channel_id,),
                )
            ).fetchone()
            if row is None or row["interruption_started_at"] is None:
                return None
            await connection.execute(
                """UPDATE chat_analytics_state
                   SET interruption_started_at=NULL, interruption_reason=NULL WHERE channel_id=?""",
                (channel_id,),
            )
            return float(row["interruption_started_at"]), str(row["interruption_reason"] or "statistics interruption")

    async def mark_member_dates_incomplete(
        self, channel_id: int, stat_dates: Sequence[str], reason: str
    ) -> None:
        if not stat_dates:
            return
        async with self.db.transaction() as connection:
            await self._mark_member_dates_incomplete(connection, channel_id, stat_dates, reason)

    async def _mark_member_dates_incomplete(self, connection, channel_id: int, stat_dates: Sequence[str], reason: str) -> None:
        for stat_date in stat_dates:
            await connection.execute(
                """INSERT INTO member_daily_stats(channel_id, stat_date, is_complete, incomplete_reason)
                   VALUES (?, ?, 0, ?)
                   ON CONFLICT(channel_id, stat_date) DO UPDATE SET
                       is_complete=0, incomplete_reason=COALESCE(member_daily_stats.incomplete_reason, excluded.incomplete_reason),
                       updated_at=CURRENT_TIMESTAMP""",
                (channel_id, stat_date, reason),
            )

    async def get_daily_member_stats(self, channel_id: int, stat_date: str | date) -> DailyMemberStats | None:
        value = stat_date.isoformat() if isinstance(stat_date, date) else stat_date
        row = await self.db.fetch_one(
            """SELECT stat_date, joined_count, left_count, is_complete, incomplete_reason
               FROM member_daily_stats WHERE channel_id=? AND stat_date=?""",
            (channel_id, value),
        )
        return None if row is None else self._daily_member_stats(row)

    @staticmethod
    def _daily_member_stats(row) -> DailyMemberStats:
        return DailyMemberStats(
            date.fromisoformat(str(row["stat_date"])),
            int(row["joined_count"]),
            int(row["left_count"]),
            bool(row["is_complete"]),
            row["incomplete_reason"],
        )

    async def get_analytics_state(self, channel_id: int):
        return await self.db.fetch_one("SELECT * FROM chat_analytics_state WHERE channel_id=?", (channel_id,))

    async def set_member_count_cache(self, channel_id: int, member_count: int, counted_at: float) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE chat_analytics_state SET last_member_count=?, last_count_at=?
                   WHERE channel_id=? AND (last_count_at IS NULL OR last_count_at <= ?)""",
                (member_count, counted_at, channel_id, counted_at),
            )
            return cursor.rowcount > 0

    async def clear_member_count_cache(self, channel_id: int) -> None:
        await self.db.execute(
            "UPDATE chat_analytics_state SET last_member_count=NULL, last_count_at=NULL WHERE channel_id=?", (channel_id,)
        )

    async def cleanup_processed_member_updates(self, before_event_at: float) -> int:
        async with self.db.transaction() as connection:
            cursor = await connection.execute("DELETE FROM processed_member_updates WHERE event_at < ?", (before_event_at,))
            return cursor.rowcount

    async def list_stats_managed_channel_ids(self) -> list[int]:
        rows = await self.db.fetch_all("SELECT channel_id FROM chat_analytics_state ORDER BY channel_id")
        return [int(row["channel_id"]) for row in rows]

    async def get_analytics_heartbeat(self) -> float | None:
        value = await self.db.fetch_value("SELECT last_heartbeat_at FROM analytics_runtime_state WHERE id=1")
        return None if value is None else float(value)

    async def set_analytics_heartbeat(self, heartbeat_at: float) -> None:
        await self.db.execute(
            """INSERT INTO analytics_runtime_state(id, last_heartbeat_at) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at""",
            (heartbeat_at,),
        )

    async def reserve_daily_report_delivery(self, user_id: int, report_date: str, due_at: float) -> bool:
        """Create one idempotent scheduled delivery for a user and local report date."""
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """INSERT OR IGNORE INTO daily_report_deliveries(user_id, report_date, status, next_attempt_at)
                   VALUES (?, ?, 'pending', ?)""",
                (user_id, report_date, due_at),
            )
            return cursor.rowcount > 0

    async def list_due_daily_report_deliveries(self, now: float, limit: int = 100) -> list[DailyReportDelivery]:
        rows = await self.db.fetch_all(
            """SELECT user_id, report_date, status, attempts, next_attempt_at, last_error, sent_at, payload_json, next_chunk_index
               FROM daily_report_deliveries
               WHERE status IN ('pending', 'retry') AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
               ORDER BY next_attempt_at, user_id, report_date LIMIT ?""",
            (now, limit),
        )
        return [self._daily_report_delivery(row) for row in rows]

    async def claim_daily_report_delivery(self, user_id: int, report_date: str, now: float) -> DailyReportDelivery | None:
        """Atomically claim a due delivery and count its send attempt."""
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries
                   SET status='sending', attempts=attempts+1, next_attempt_at=NULL
                   WHERE user_id=? AND report_date=? AND status IN ('pending', 'retry')
                     AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?""",
                (user_id, report_date, now),
            )
            if not cursor.rowcount:
                return None
            row = await (
                await connection.execute(
                    """SELECT user_id, report_date, status, attempts, next_attempt_at, last_error, sent_at, payload_json, next_chunk_index
                       FROM daily_report_deliveries WHERE user_id=? AND report_date=?""",
                    (user_id, report_date),
                )
            ).fetchone()
            return self._daily_report_delivery(row)

    async def store_daily_report_payload(self, user_id: int, report_date: str, payload_json: str) -> bool:
        """Persist the immutable chunks before the first private-message attempt."""
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries SET payload_json=?, next_chunk_index=0
                   WHERE user_id=? AND report_date=? AND status='sending' AND payload_json IS NULL""",
                (payload_json, user_id, report_date),
            )
            return cursor.rowcount > 0

    async def advance_daily_report_delivery_chunk(self, user_id: int, report_date: str, next_chunk_index: int) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries SET next_chunk_index=?
                   WHERE user_id=? AND report_date=? AND status='sending' AND next_chunk_index < ?""",
                (next_chunk_index, user_id, report_date, next_chunk_index),
            )
            return cursor.rowcount > 0

    async def replace_daily_report_payload(self, user_id: int, report_date: str, payload_json: str) -> bool:
        """Replace only unsent chunk metadata after a definitive permission loss."""
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries SET payload_json=?
                   WHERE user_id=? AND report_date=? AND status='sending'""",
                (payload_json, user_id, report_date),
            )
            return cursor.rowcount > 0

    async def record_daily_report_delivery_failure(
        self, user_id: int, report_date: str, error: str, next_attempt_at: float
    ) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries
                   SET status='retry', next_attempt_at=?, last_error=?, sent_at=NULL
                   WHERE user_id=? AND report_date=? AND status='sending'""",
                (next_attempt_at, error[:1000], user_id, report_date),
            )
            return cursor.rowcount > 0

    async def mark_daily_report_delivery_sent(self, user_id: int, report_date: str, sent_at: float) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries
                   SET status='sent', next_attempt_at=NULL, last_error=NULL, sent_at=?
                   WHERE user_id=? AND report_date=? AND status='sending'""",
                (sent_at, user_id, report_date),
            )
            return cursor.rowcount > 0

    async def mark_daily_report_delivery_terminal(self, user_id: int, report_date: str, error: str) -> bool:
        """Stop retrying a report that cannot be delivered to this private chat."""
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries
                   SET status='terminal', next_attempt_at=NULL, last_error=?
                   WHERE user_id=? AND report_date=? AND status='sending'""",
                (error[:1000], user_id, report_date),
            )
            return cursor.rowcount > 0

    async def recover_stuck_daily_report_deliveries(self, now: float) -> int:
        """Make deliveries left in sending state by a terminated process due again."""
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE daily_report_deliveries
                   SET status='retry', next_attempt_at=?, last_error=COALESCE(last_error, 'recovered interrupted delivery')
                   WHERE status='sending'""",
                (now,),
            )
            return cursor.rowcount

    @staticmethod
    def _daily_report_delivery(row) -> DailyReportDelivery:
        return DailyReportDelivery(
            int(row["user_id"]),
            str(row["report_date"]),
            str(row["status"]),
            int(row["attempts"]),
            None if row["next_attempt_at"] is None else float(row["next_attempt_at"]),
            row["last_error"],
            None if row["sent_at"] is None else float(row["sent_at"]),
            row["payload_json"],
            int(row["next_chunk_index"]),
        )

    async def health_counts(self) -> dict[str, int | float | None]:
        names = ("users", "channels", "drafts", "refresh_jobs")
        counts: dict[str, int | float | None] = {
            name: int(await self.db.fetch_value(f"SELECT COUNT(*) FROM {name}") or 0) for name in names
        }
        counts["analytics_incomplete_days"] = int(
            await self.db.fetch_value("SELECT COUNT(*) FROM member_daily_stats WHERE is_complete=0") or 0
        )
        counts["daily_report_deliveries_failed"] = int(
            await self.db.fetch_value("SELECT COUNT(*) FROM daily_report_deliveries WHERE status IN ('retry', 'terminal')") or 0
        )
        counts["daily_report_deliveries_due"] = int(
            await self.db.fetch_value(
                "SELECT COUNT(*) FROM daily_report_deliveries WHERE status IN ('pending', 'retry') AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?",
                (time.time(),),
            )
            or 0
        )
        heartbeat = await self.get_analytics_heartbeat()
        counts["analytics_last_heartbeat_at"] = heartbeat
        return counts

    async def list_collectable_storage_ids(self, limit: int = 100) -> list[int]:
        rows = await self.db.fetch_all(
            """SELECT DISTINCT candidate.storage_message_id
               FROM content_items candidate
               WHERE candidate.storage_message_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM collected_storage_messages done
                     WHERE done.message_id=candidate.storage_message_id
                 )
                 AND NOT EXISTS (
                     SELECT 1
                     FROM content_items live
                     JOIN draft_revisions revision ON revision.id=live.revision_id
                     JOIN drafts draft ON draft.id=revision.draft_id
                     WHERE live.storage_message_id=candidate.storage_message_id
                       AND (
                           (draft.deleted_at IS NULL AND draft.current_revision_id=revision.id)
                           OR EXISTS (SELECT 1 FROM channel_slots slot WHERE slot.revision_id=revision.id)
                       )
                 )
               ORDER BY candidate.storage_message_id
               LIMIT ?""",
            (limit,),
        )
        return [int(row["storage_message_id"]) for row in rows]

    async def mark_storage_collected(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        async with self.db.transaction() as connection:
            await connection.executemany(
                "INSERT OR IGNORE INTO collected_storage_messages(message_id) VALUES (?)",
                [(message_id,) for message_id in message_ids],
            )

    @staticmethod
    def _job_from_row(row) -> RefreshJob:
        return RefreshJob(
            channel_id=int(row["channel_id"]),
            due_at=float(row["due_at"]),
            generation=int(row["generation"]),
            attempts=int(row["attempts"]),
            reason=str(row["reason"]),
            last_error=row["last_error"],
        )
