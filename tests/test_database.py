import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from bottom_post_bot import database
from bottom_post_bot.database import Database
from bottom_post_bot.domain import ButtonSpec, ContentItem
from bottom_post_bot.handlers import BotHandlers
from bottom_post_bot.repositories import AuthorizationError, Repository, ResourceLimitError


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repo = Repository(self.db)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_migration_enables_foreign_keys_and_wal(self) -> None:
        foreign_keys = await self.db.fetch_value("PRAGMA foreign_keys")
        journal_mode = await self.db.fetch_value("PRAGMA journal_mode")
        version = await self.db.fetch_value("SELECT MAX(version) FROM schema_migrations")
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(version, 6)

    async def test_migration_four_backfills_slot_names_from_existing_drafts(self) -> None:
        await self.db.close()
        database_path = Path(self.tempdir.name) / "legacy.sqlite3"
        self._create_version_three_database(database_path)

        self.db = await Database.open(database_path)

        self.assertEqual(await self.db.fetch_value("SELECT MAX(version) FROM schema_migrations"), 6)
        slot = await self.db.fetch_one(
            "SELECT display_name, name_customized FROM channel_slots WHERE channel_id=? AND slot_number=?",
            (-1009, 1),
        )
        self.assertEqual(dict(slot), {"display_name": "Existing draft", "name_customized": 0})
        self.assertIsNotNone(await self.db.fetch_one("SELECT name FROM sqlite_master WHERE name='pending_drafts'"))

    async def test_migration_four_rolls_back_every_statement_when_one_fails(self) -> None:
        await self.db.close()
        database_path = Path(self.tempdir.name) / "legacy.sqlite3"
        self._create_version_three_database(database_path)
        connection = await aiosqlite.connect(database_path)
        connection.row_factory = aiosqlite.Row
        self.db = Database(connection)
        await self.db._configure()

        original_migration = database.MIGRATION_4
        database.MIGRATION_4 = (*original_migration[:-1], "THIS IS NOT VALID SQL")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                await self.db._migrate()
        finally:
            database.MIGRATION_4 = original_migration

        self.assertEqual(await self.db.fetch_value("SELECT MAX(version) FROM schema_migrations"), 3)
        self.assertIsNone(await self.db.fetch_one("SELECT name FROM sqlite_master WHERE name='pending_drafts'"))
        columns = await self.db.fetch_all("PRAGMA table_info(channel_slots)")
        self.assertNotIn("display_name", {column["name"] for column in columns})
        self.assertNotIn("name_customized", {column["name"] for column in columns})

    async def test_migration_five_quarantines_legacy_invalid_urls_without_breaking_active_loads(self) -> None:
        await self.db.close()
        database_path = Path(self.tempdir.name) / "legacy-v4.sqlite3"
        self._create_version_four_database(database_path)
        connection = sqlite3.connect(database_path)
        try:
            revision_id = connection.execute("SELECT id FROM draft_revisions").fetchone()[0]
            draft_id = connection.execute("SELECT id FROM drafts").fetchone()[0]
            connection.execute(
                "INSERT INTO content_items(revision_id, position, text) VALUES (?, ?, ?)",
                (revision_id, 0, "active body"),
            )
            connection.execute(
                "UPDATE drafts SET current_revision_id=? WHERE id=?",
                (revision_id, draft_id),
            )
            valid_id = connection.execute(
                """INSERT INTO draft_buttons(revision_id, row_number, column_number, text, url)
                   VALUES (?, ?, ?, ?, ?)""",
                (revision_id, 0, 0, "Valid", "HTTPS://example.com/path"),
            ).lastrowid
            invalid_ids = [
                connection.execute(
                    """INSERT INTO draft_buttons(revision_id, row_number, column_number, text, url)
                       VALUES (?, ?, ?, ?, ?)""",
                    (revision_id, 0, column, text, url),
                ).lastrowid
                for column, text, url in (
                    (1, "Opaque HTTPS", "https:foo"),
                    (2, "Opaque Telegram", "tg:foo"),
                    (3, "Malformed authority", "https://["),
                )
            ]
            connection.commit()
        finally:
            connection.close()

        self.db = await Database.open(database_path)
        self.repo = Repository(self.db)

        self.assertEqual(await self.db.fetch_value("SELECT MAX(version) FROM schema_migrations"), 6)
        active_buttons = await self.db.fetch_all("SELECT id, url FROM draft_buttons ORDER BY id")
        self.assertEqual([(row["id"], row["url"]) for row in active_buttons], [(valid_id, "HTTPS://example.com/path")])
        quarantined = await self.db.fetch_all(
            """SELECT original_button_id, revision_id, row_number, column_number, text, url, reason, quarantined_at
               FROM quarantined_draft_buttons ORDER BY original_button_id"""
        )
        self.assertEqual([row["original_button_id"] for row in quarantined], invalid_ids)
        self.assertEqual([row["url"] for row in quarantined], ["https:foo", "tg:foo", "https://["])
        self.assertTrue(all("https://" in row["reason"] for row in quarantined))
        self.assertTrue(all(row["quarantined_at"] for row in quarantined))

        draft = await self.repo.get_draft(1, draft_id)
        slots = await self.repo.list_channel_slots(-1009)
        publish_slots, _, _ = await self.repo.load_publish_state(-1009)
        self.assertEqual([button.url for button in draft.current_revision.buttons], ["HTTPS://example.com/path"])
        self.assertEqual([button.url for button in slots[0].revision.buttons], ["HTTPS://example.com/path"])
        self.assertEqual([button.url for button in publish_slots[0].revision.buttons], ["HTTPS://example.com/path"])

    async def test_migration_six_adds_analytics_schema_from_version_five(self) -> None:
        await self.db.close()
        database_path = Path(self.tempdir.name) / "legacy-v5.sqlite3"
        self._create_version_four_database(database_path)
        connection = sqlite3.connect(database_path)
        try:
            for statement in database.MIGRATION_5:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (5)")
            connection.commit()
        finally:
            connection.close()

        self.db = await Database.open(database_path)

        self.assertEqual(await self.db.fetch_value("SELECT MAX(version) FROM schema_migrations"), 6)
        columns = {row["name"] for row in await self.db.fetch_all("PRAGMA table_info(channels)")}
        self.assertIn("chat_type", columns)
        manager_columns = {row["name"] for row in await self.db.fetch_all("PRAGMA table_info(channel_managers)")}
        self.assertIn("stats_push_enabled", manager_columns)
        self.assertIsNotNone(await self.db.fetch_one("SELECT name FROM sqlite_master WHERE name='member_daily_stats'"))
        self.assertIsNotNone(await self.db.fetch_one("SELECT name FROM sqlite_master WHERE name='processed_member_updates'"))
        for table, expected_columns in {
            "member_daily_stats": {"channel_id", "stat_date", "joined_count", "left_count", "is_complete", "incomplete_reason", "updated_at"},
            "processed_member_updates": {"update_id", "channel_id", "direction", "event_at", "processed_at"},
            "chat_analytics_state": {"channel_id", "started_at", "last_member_count", "last_count_at"},
            "analytics_runtime_state": {"id", "last_heartbeat_at"},
            "daily_report_deliveries": {"user_id", "report_date", "status", "attempts", "next_attempt_at", "last_error", "sent_at", "payload_json", "next_chunk_index"},
        }.items():
            with self.subTest(table=table):
                columns = {row["name"] for row in await self.db.fetch_all(f"PRAGMA table_info({table})")}
                self.assertTrue(expected_columns <= columns)
        indexes = {row["name"] for row in await self.db.fetch_all("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_processed_member_updates_event_at", indexes)
        self.assertIn("idx_daily_report_deliveries_due", indexes)

    async def test_migration_six_rolls_back_every_statement_when_one_fails(self) -> None:
        await self.db.close()
        database_path = Path(self.tempdir.name) / "legacy-v5-rollback.sqlite3"
        self._create_version_four_database(database_path)
        connection = sqlite3.connect(database_path)
        try:
            for statement in database.MIGRATION_5:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (5)")
            connection.commit()
        finally:
            connection.close()
        connection = await aiosqlite.connect(database_path)
        connection.row_factory = aiosqlite.Row
        self.db = Database(connection)
        await self.db._configure()
        original_migration = database.MIGRATION_6
        database.MIGRATION_6 = (*original_migration[:-1], "THIS IS NOT VALID SQL")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                await self.db._migrate()
        finally:
            database.MIGRATION_6 = original_migration

        self.assertEqual(await self.db.fetch_value("SELECT MAX(version) FROM schema_migrations"), 5)
        self.assertIsNone(await self.db.fetch_one("SELECT name FROM sqlite_master WHERE name='member_daily_stats'"))

    async def test_migration_five_rolls_back_quarantine_and_deletion_when_validation_fails(self) -> None:
        await self.db.close()
        database_path = Path(self.tempdir.name) / "legacy-v4-rollback.sqlite3"
        self._create_version_four_database(database_path)
        connection = sqlite3.connect(database_path)
        try:
            revision_id = connection.execute("SELECT id FROM draft_revisions").fetchone()[0]
            connection.executemany(
                """INSERT INTO draft_buttons(revision_id, row_number, column_number, text, url)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (revision_id, 0, 0, "First", "https:foo"),
                    (revision_id, 0, 1, "Second", "tg:foo"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        connection = await aiosqlite.connect(database_path)
        connection.row_factory = aiosqlite.Row
        self.db = Database(connection)
        await self.db._configure()
        original_validator = database.normalize_button_url
        validation_calls = 0

        def fail_during_second_validation(url: str) -> str:
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 2:
                raise RuntimeError("injected migration failure")
            return original_validator(url)

        with patch.object(database, "normalize_button_url", side_effect=fail_during_second_validation):
            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                await self.db._migrate()

        self.assertEqual(await self.db.fetch_value("SELECT MAX(version) FROM schema_migrations"), 4)
        self.assertEqual(await self.db.fetch_value("SELECT COUNT(*) FROM draft_buttons"), 2)
        self.assertIsNone(
            await self.db.fetch_one("SELECT name FROM sqlite_master WHERE name='quarantined_draft_buttons'")
        )

    async def test_personal_drafts_are_isolated_by_owner(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_user(2, "Bob")
        draft = await self.repo.create_draft(
            owner_user_id=1,
            name="广告",
            items=(ContentItem(text="hello"),),
            buttons=(ButtonSpec("网站", "https://example.com", 0, 0),),
            max_drafts=50,
        )
        self.assertIsNotNone(await self.repo.get_draft(1, draft.id))
        self.assertIsNone(await self.repo.get_draft(2, draft.id))

    async def test_draft_quota_is_enforced(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.create_draft(1, "one", (ContentItem(text="one"),), (), max_drafts=1)
        with self.assertRaises(ResourceLimitError):
            await self.repo.create_draft(1, "two", (ContentItem(text="two"),), (), max_drafts=1)

    async def test_bind_manager_returns_whether_the_transaction_created_the_association(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_channel(-1009, "Channel", None)

        self.assertTrue(await self.repo.bind_manager(1, -1009, max_channels=10))
        self.assertFalse(await self.repo.bind_manager(1, -1009, max_channels=10))

    async def test_pending_draft_confirmation_preserves_album_order_and_is_one_time(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        pending = await self.repo.create_pending_draft(
            1,
            (
                ContentItem(text="caption", storage_message_id=700, grouped_id="g"),
                ContentItem(storage_message_id=701, media_kind="photo", telegram_file_id="f", grouped_id="g"),
            ),
            expires_at=200.0,
        )

        draft = await self.repo.confirm_pending_draft(1, pending.id, "Album", 50, now=100.0)

        self.assertEqual([item.storage_message_id for item in draft.current_revision.items], [700, 701])
        self.assertIsNone(await self.repo.get_pending_draft(1, pending.id))
        with self.assertRaisesRegex(AuthorizationError, "processed or expired"):
            await self.repo.confirm_pending_draft(1, pending.id, "Again", 50, now=100.0)

    async def test_pending_draft_quota_error_preserves_pending_without_creating_draft(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.create_draft(1, "existing", (ContentItem(text="existing"),), (), max_drafts=1)
        pending = await self.repo.create_pending_draft(1, (ContentItem(text="later", storage_message_id=700),), 200.0)

        with self.assertRaises(ResourceLimitError):
            await self.repo.confirm_pending_draft(1, pending.id, "later", 1, now=100.0)

        self.assertIsNotNone(await self.repo.get_pending_draft(1, pending.id))
        self.assertEqual(len(await self.repo.list_drafts(1)), 1)

    async def test_foreign_user_cannot_read_confirm_or_discard_pending_draft(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_user(2, "Bob")
        pending = await self.repo.create_pending_draft(1, (ContentItem(text="private", storage_message_id=700),), 200.0)

        self.assertIsNone(await self.repo.get_pending_draft(2, pending.id))
        with self.assertRaisesRegex(AuthorizationError, "processed or expired"):
            await self.repo.confirm_pending_draft(2, pending.id, "Nope", 50, now=100.0)
        self.assertFalse(await self.repo.mark_pending_discarded(2, pending.id))
        self.assertIsNotNone(await self.repo.get_pending_draft(1, pending.id))

    async def test_slot_keeps_revision_snapshot_when_draft_changes(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_channel(-1009, "Channel", None)
        await self.repo.bind_manager(1, -1009, max_channels=10)
        draft = await self.repo.create_draft(1, "ad", (ContentItem(text="old"),), (), max_drafts=50)
        await self.repo.assign_slot(-1009, 1, draft.current_revision.id, actor_id=1, max_slots=10)
        await self.repo.create_revision(draft.id, 1, (ContentItem(text="new"),), ())

        slots, _, _ = await self.repo.load_publish_state(-1009)

        self.assertEqual(slots[0].revision.items[0].text, "old")

    async def test_owned_draft_name_for_revision_requires_the_active_owner_draft(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_user(2, "Bob")
        draft = await self.repo.create_draft(1, "First", (ContentItem(text="first"),), (), max_drafts=50)

        self.assertEqual(await self.repo.owned_draft_name_for_revision(draft.current_revision.id, 1), "First")
        self.assertIsNone(await self.repo.owned_draft_name_for_revision(draft.current_revision.id, 2))

    async def test_slot_reads_include_name_metadata_for_management_without_affecting_publish_snapshot(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_channel(-1009, "Channel", None)
        await self.repo.bind_manager(1, -1009, max_channels=10)
        draft = await self.repo.create_draft(1, "First", (ContentItem(text="first"),), (), max_drafts=50)
        await self.repo.assign_slot(-1009, 1, draft.current_revision.id, actor_id=1, max_slots=10, display_name="First")

        listed = await self.repo.list_channel_slots(-1009)
        published, _, _ = await self.repo.load_publish_state(-1009)

        self.assertEqual((listed[0].display_name, listed[0].name_customized), ("First", False))
        self.assertEqual((published[0].display_name, published[0].name_customized), ("First", False))

    async def test_repository_rename_slot_trims_validates_and_audits_final_name(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_channel(-1009, "Channel", None)
        await self.repo.bind_manager(1, -1009, max_channels=10)
        draft = await self.repo.create_draft(1, "First", (ContentItem(text="first"),), (), max_drafts=50)
        await self.repo.assign_slot(-1009, 1, draft.current_revision.id, actor_id=1, max_slots=10, display_name="First")

        for invalid_name in ("   ", "x" * 101):
            with self.assertRaisesRegex(ValueError, "1 到 100"):
                await self.repo.rename_slot(-1009, 1, invalid_name, 1)

        await self.repo.rename_slot(-1009, 1, " 首页入口 ", 1)

        slot = (await self.repo.list_channel_slots(-1009))[0]
        audit = await self.db.fetch_one("SELECT detail_json FROM audit_logs WHERE action='slot.rename' ORDER BY id DESC")
        self.assertEqual((slot.display_name, slot.name_customized), ("首页入口", True))
        self.assertEqual(json.loads(audit["detail_json"]), {"slot": 1, "name": "首页入口"})

    async def test_recovery_turns_incomplete_batch_into_cleanup_and_refresh_work(self) -> None:
        await self.repo.upsert_channel(-1009, "Channel", None)
        batch_id = await self.repo.begin_batch(-1009)
        await self.repo.record_batch_messages(batch_id, [301, 302])

        recovered = await self.repo.recover_incomplete_batches(now=123.0)
        slots, cleanup_ids, _ = await self.repo.load_publish_state(-1009)
        job = await self.repo.get_refresh_job(-1009)

        self.assertEqual(recovered, 1)
        self.assertEqual(slots, [])
        self.assertEqual(cleanup_ids, [301, 302])
        self.assertEqual(job.due_at, 123.0)

    async def test_disabled_channel_still_returns_old_messages_for_cleanup(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_channel(-1009, "Channel", None)
        await self.repo.bind_manager(1, -1009, max_channels=10)
        await self.repo.commit_batch(-1009, [401])
        await self.repo.set_channel_options(-1009, 1, enabled=False)

        slots, cleanup_ids, _ = await self.repo.load_publish_state(-1009)

        self.assertEqual(slots, [])
        self.assertEqual(cleanup_ids, [401])

    async def test_storage_media_is_collectable_only_after_all_live_references_are_removed(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_channel(-1009, "Channel", None)
        await self.repo.bind_manager(1, -1009, max_channels=10)
        draft = await self.repo.create_draft(
            1,
            "media",
            (ContentItem(storage_message_id=701, media_kind="photo", telegram_file_id="photo-file"),),
            (),
            max_drafts=50,
        )
        await self.repo.assign_slot(-1009, 1, draft.current_revision.id, 1, max_slots=10)
        await self.repo.delete_draft(1, draft.id)
        self.assertEqual(await self.repo.list_collectable_storage_ids(), [])

        await self.repo.clear_slot(-1009, 1, 1)
        self.assertEqual(await self.repo.list_collectable_storage_ids(), [701])

    async def test_message_in_sending_batch_is_ignored_by_channel_listener(self) -> None:
        await self.repo.upsert_channel(-1009, "Channel", None)
        batch_id = await self.repo.begin_batch(-1009)
        await self.repo.record_batch_messages(batch_id, [888])
        self.assertTrue(await self.repo.is_current_sent_message(-1009, 888))

    async def test_finalize_batch_preserves_concurrent_access_loss_pause_for_recovery(self) -> None:
        await self.repo.upsert_user(1, "Alice")
        await self.repo.upsert_channel(-1009, "Channel", None)
        await self.repo.bind_manager(1, -1009, max_channels=10)
        batch_id = await self.repo.begin_batch(-1009)
        await self.repo.record_batch_messages(batch_id, [901])

        await self.repo.pause_channel(-1009, "bot was removed during publish")
        await self.repo.finalize_batch(-1009, batch_id)

        channel = await self.repo.get_channel(-1009)
        self.assertEqual(
            (channel["status"], channel["enabled"], channel["last_error"]),
            ("paused", 0, "bot was removed during publish"),
        )
        self.assertTrue(await self.repo.is_bound_manager(1, -1009))

        handlers = BotHandlers(
            SimpleNamespace(),
            self.repo,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(assert_user_can_manage=AsyncMock()),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(max_slots_per_channel=1),
            pending_drafts=SimpleNamespace(),
        )
        handlers._show = AsyncMock()
        await handlers._show_channel(SimpleNamespace(), 1, -1009)
        rows = handlers._show.await_args.args[2]
        callbacks = [button.callback_data for row in rows for button in row]
        self.assertIn("c:resume:-1009", callbacks)

    async def test_finalize_batch_activates_channel_after_normal_success(self) -> None:
        await self.repo.upsert_channel(-1009, "Channel", None)
        await self.repo.mark_failure(-1009, "old transient failure")
        batch_id = await self.repo.begin_batch(-1009)

        await self.repo.finalize_batch(-1009, batch_id)

        channel = await self.repo.get_channel(-1009)
        self.assertEqual((channel["status"], channel["last_error"]), ("active", None))

    @staticmethod
    def _create_version_three_database(database_path: Path) -> None:
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(database.MIGRATION_1)
            connection.executescript(database.MIGRATION_2)
            connection.executescript(database.MIGRATION_3)
            connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            connection.executemany("INSERT INTO schema_migrations(version) VALUES (?)", [(1,), (2,), (3,)])
            connection.execute("INSERT INTO users(id, display_name) VALUES (?, ?)", (1, "Alice"))
            connection.execute("INSERT INTO channels(id, title) VALUES (?, ?)", (-1009, "Channel"))
            cursor = connection.execute("INSERT INTO drafts(owner_user_id, name) VALUES (?, ?)", (1, "Existing draft"))
            draft_id = cursor.lastrowid
            cursor = connection.execute(
                "INSERT INTO draft_revisions(draft_id, revision_number) VALUES (?, ?)",
                (draft_id, 1),
            )
            connection.execute(
                "INSERT INTO channel_slots(channel_id, slot_number, revision_id, updated_by) VALUES (?, ?, ?, ?)",
                (-1009, 1, cursor.lastrowid, 1),
            )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def _create_version_four_database(cls, database_path: Path) -> None:
        cls._create_version_three_database(database_path)
        connection = sqlite3.connect(database_path)
        try:
            for statement in database.MIGRATION_4:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (4)")
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
