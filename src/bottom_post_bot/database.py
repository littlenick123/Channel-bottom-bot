from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

from .domain import ValidationError, normalize_button_url


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    silent INTEGER NOT NULL DEFAULT 1,
    refresh_delay_seconds INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL DEFAULT 'active',
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS channel_managers (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    bound_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    current_revision_id INTEGER,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS draft_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (draft_id, revision_number)
);
CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES draft_revisions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    text TEXT,
    storage_message_id INTEGER,
    media_kind TEXT,
    grouped_id TEXT,
    formatting_entities_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (revision_id, position)
);
CREATE TABLE IF NOT EXISTS draft_buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES draft_revisions(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    column_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    url TEXT NOT NULL,
    UNIQUE (revision_id, row_number, column_number)
);
CREATE TABLE IF NOT EXISTS channel_slots (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    slot_number INTEGER NOT NULL,
    revision_id INTEGER NOT NULL REFERENCES draft_revisions(id),
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_by INTEGER NOT NULL REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, slot_number)
);
CREATE TABLE IF NOT EXISTS sent_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS sent_messages (
    batch_id INTEGER NOT NULL REFERENCES sent_batches(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (batch_id, message_id)
);
CREATE TABLE IF NOT EXISTS orphan_messages (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS refresh_jobs (
    channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    due_at REAL NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS conversation_states (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_drafts_owner ON drafts(owner_user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_revisions_draft ON draft_revisions(draft_id, revision_number);
CREATE INDEX IF NOT EXISTS idx_batches_current ON sent_batches(channel_id, is_current);
CREATE INDEX IF NOT EXISTS idx_audit_channel ON audit_logs(channel_id, created_at);
"""

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS collected_storage_messages (
    message_id INTEGER PRIMARY KEY,
    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

MIGRATION_3 = """
ALTER TABLE content_items ADD COLUMN telegram_file_id TEXT;
"""

MIGRATION_4 = (
    """
    CREATE TABLE pending_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        expires_at REAL NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE pending_draft_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pending_draft_id INTEGER NOT NULL REFERENCES pending_drafts(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        text TEXT,
        storage_message_id INTEGER,
        media_kind TEXT,
        telegram_file_id TEXT,
        grouped_id TEXT,
        formatting_entities_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE (pending_draft_id, position)
    )
    """,
    "ALTER TABLE channel_slots ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE channel_slots ADD COLUMN name_customized INTEGER NOT NULL DEFAULT 0",
    """
    UPDATE channel_slots AS slot
    SET display_name = COALESCE(
        (
            SELECT draft.name
            FROM draft_revisions AS revision
            JOIN drafts AS draft ON draft.id = revision.draft_id
            WHERE revision.id = slot.revision_id
        ),
        printf('置底帖子 %d', slot.slot_number)
    )
    """,
    "CREATE INDEX idx_pending_drafts_status_expires_at ON pending_drafts(status, expires_at)",
)

MIGRATION_5 = (
    """
    CREATE TABLE quarantined_draft_buttons (
        original_button_id INTEGER PRIMARY KEY,
        revision_id INTEGER NOT NULL,
        row_number INTEGER NOT NULL,
        column_number INTEGER NOT NULL,
        text TEXT NOT NULL,
        url TEXT NOT NULL,
        reason TEXT NOT NULL,
        quarantined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
)


class Database:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = aiosqlite.Row
        database = cls(connection)
        await database._configure()
        await database._migrate()
        return database

    async def _configure(self) -> None:
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA busy_timeout = 5000")
        await self.connection.commit()

    async def _migrate(self) -> None:
        async with self._lock:
            await self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            row = await (await self.connection.execute("SELECT MAX(version) AS version FROM schema_migrations")).fetchone()
            version = int(row["version"] or 0)
            try:
                if version < 1:
                    await self.connection.executescript(MIGRATION_1)
                    await self.connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
                if version < 2:
                    await self.connection.executescript(MIGRATION_2)
                    await self.connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
                if version < 3:
                    await self.connection.executescript(MIGRATION_3)
                    await self.connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")
                await self.connection.commit()
                if version < 4:
                    await self.connection.execute("BEGIN IMMEDIATE")
                    for statement in MIGRATION_4:
                        await self.connection.execute(statement)
                    await self.connection.execute("INSERT INTO schema_migrations(version) VALUES (4)")
                await self.connection.commit()
                if version < 5:
                    await self.connection.execute("BEGIN IMMEDIATE")
                    for statement in MIGRATION_5:
                        await self.connection.execute(statement)
                    cursor = await self.connection.execute(
                        """SELECT id, revision_id, row_number, column_number, text, url
                           FROM draft_buttons ORDER BY id"""
                    )
                    try:
                        button_rows = await cursor.fetchall()
                    finally:
                        await cursor.close()
                    for button in button_rows:
                        try:
                            normalize_button_url(str(button["url"]))
                        except ValidationError as exc:
                            await self.connection.execute(
                                """INSERT INTO quarantined_draft_buttons(
                                       original_button_id, revision_id, row_number, column_number,
                                       text, url, reason
                                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    button["id"],
                                    button["revision_id"],
                                    button["row_number"],
                                    button["column_number"],
                                    button["text"],
                                    button["url"],
                                    str(exc),
                                ),
                            )
                            await self.connection.execute(
                                "DELETE FROM draft_buttons WHERE id=?",
                                (button["id"],),
                            )
                    await self.connection.execute("INSERT INTO schema_migrations(version) VALUES (5)")
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def execute(self, sql: str, parameters: Iterable[Any] = ()) -> None:
        async with self._lock:
            await self.connection.execute(sql, tuple(parameters))
            await self.connection.commit()

    async def fetch_one(self, sql: str, parameters: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self.connection.execute(sql, tuple(parameters))
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def fetch_all(self, sql: str, parameters: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.connection.execute(sql, tuple(parameters))
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def fetch_value(self, sql: str, parameters: Iterable[Any] = ()) -> Any:
        row = await self.fetch_one(sql, parameters)
        return None if row is None else row[0]

    async def close(self) -> None:
        await self.connection.close()
