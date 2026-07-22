import json
import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe

from bottom_post_bot.aiogram_gateway import BotApiPermissionGateway
from bottom_post_bot.app import build_router
from bottom_post_bot.analytics import AnalyticsService, MemberUpdateAdapter
from bottom_post_bot.channels import ChannelIdentity, ChannelService
from bottom_post_bot.database import Database
from bottom_post_bot.domain import ContentItem
from bottom_post_bot.membership import ChatMembershipService
from bottom_post_bot.notifications import TelegramAdminNotifier
from bottom_post_bot.permissions import BotCapabilities, PermissionService
from bottom_post_bot.repositories import Repository


class FakePermissionGateway:
    def __init__(self) -> None:
        self.user_admin = True
        self.bot_ready = True
        self.channels: dict[int, ChannelIdentity] = {}
        self.bot_capability_calls = 0
        self.user_error: Exception | None = None
        self.bot_capability_outcomes: list[BotCapabilities | Exception] = []

    async def resolve_channel(self, channel_id: int) -> ChannelIdentity:
        return self.channels[channel_id]

    async def user_is_admin(self, channel_id: int, user_id: int) -> bool:
        if self.user_error is not None:
            raise self.user_error
        return self.user_admin

    async def bot_capabilities(self, channel_id: int) -> BotCapabilities:
        self.bot_capability_calls += 1
        if self.bot_capability_outcomes:
            outcome = self.bot_capability_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return BotCapabilities(self.bot_ready, self.bot_ready, self.bot_ready)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.error: Exception | None = None

    async def send_message(self, **kwargs):
        if self.error:
            raise self.error
        self.messages.append(kwargs)


def channel_event(
    *,
    channel_id: int = -1007,
    title: str = "News",
    username: str | None = "news",
    chat_type: str = "channel",
    actor_id: int = 42,
    actor_name: str = "Alice",
    old_status: object = "member",
    new_status: object = "administrator",
):
    return SimpleNamespace(
        chat=SimpleNamespace(id=channel_id, title=title, username=username, type=chat_type),
        from_user=SimpleNamespace(id=actor_id, full_name=actor_name, username=actor_name.lower()),
        old_chat_member=SimpleNamespace(status=old_status),
        new_chat_member=SimpleNamespace(status=new_status),
        date=datetime(2026, 1, 2, tzinfo=UTC),
    )


class ChatMembershipServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repository = Repository(self.db)
        self.gateway = FakePermissionGateway()
        self.gateway.channels[-1007] = ChannelIdentity(-1007, "News", "news")
        self.gateway.channels[-1008] = ChannelIdentity(-1008, "Other", "other")
        self.channels = ChannelService(
            self.repository,
            PermissionService(self.repository, self.gateway),
            max_channels=1,
            max_slots=10,
            storage_channel_id=-10050,
            default_refresh_delay=17,
        )
        self.bot = FakeBot()
        self.notifier = TelegramAdminNotifier(self.bot, self.repository, self.gateway)
        self.analytics = AnalyticsService(self.repository, SimpleNamespace())
        self.membership = ChatMembershipService(
            self.repository, self.channels, self.notifier, storage_channel_id=-10050, analytics=self.analytics
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def audit_rows(self, action: str) -> list[dict]:
        rows = await self.db.fetch_all("SELECT * FROM audit_logs WHERE action=? ORDER BY id", (action,))
        return [dict(row) | {"details": json.loads(row["detail_json"])} for row in rows]

    async def test_administrator_promotion_records_binds_audits_and_notifies(self) -> None:
        await self.membership.handle(channel_event())

        channel = await self.repository.get_channel(-1007)
        self.assertEqual((channel["title"], channel["refresh_delay_seconds"]), ("News", 17))
        self.assertTrue(await self.repository.is_bound_manager(42, -1007))
        self.assertEqual(len(await self.audit_rows("channel.auto_bind")), 1)
        self.assertEqual(self.bot.messages, [{"chat_id": 42, "text": "已自动绑定频道“News”（ID: -1007）。"}])

    async def test_aiogram_channel_type_enum_is_processed(self) -> None:
        await self.membership.handle(channel_event(chat_type=ChatType.CHANNEL))

        self.assertTrue(await self.repository.is_bound_manager(42, -1007))

    async def test_duplicate_promotion_does_not_bind_or_notify_again(self) -> None:
        await self.membership.handle(channel_event())
        await self.membership.handle(channel_event(old_status="administrator"))

        self.assertEqual(len(await self.audit_rows("channel.auto_bind")), 1)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_already_bound_administrator_rechecks_lost_bot_capabilities_and_pauses(self) -> None:
        await self.membership.handle(channel_event())
        self.gateway.bot_ready = False

        await self.membership.handle(channel_event(old_status="administrator"))

        channel = await self.repository.get_channel(-1007)
        self.assertEqual((channel["status"], channel["enabled"]), ("paused", 0))
        self.assertEqual(self.gateway.bot_capability_calls, 3)
        self.assertEqual(len(await self.audit_rows("channel.bind")), 1)
        self.assertEqual(len(await self.audit_rows("channel.auto_bind")), 1)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_concurrent_duplicate_promotions_bind_audit_and_notify_once(self) -> None:
        await asyncio.gather(self.membership.handle(channel_event()), self.membership.handle(channel_event()))

        self.assertEqual(await self.repository.list_manager_ids(-1007), [42])
        self.assertEqual(len(await self.audit_rows("channel.bind")), 1)
        self.assertEqual(len(await self.audit_rows("channel.auto_bind")), 1)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_storage_channel_and_basic_group_are_ignored(self) -> None:
        for event in (
            channel_event(channel_id=-10050),
            channel_event(channel_id=-1008, chat_type="group"),
        ):
            await self.membership.handle(event)

        self.assertIsNone(await self.repository.get_channel(-10050))
        self.assertIsNone(await self.repository.get_channel(-1008))

    async def test_supergroup_promotion_binds_persists_type_and_initializes_analytics(self) -> None:
        self.gateway.channels[-1008] = ChannelIdentity(-1008, "Group", "group", "supergroup")

        await self.membership.handle(channel_event(channel_id=-1008, title="Group", username="group", chat_type="supergroup"))

        self.assertTrue(await self.repository.is_bound_manager(42, -1008))
        self.assertEqual((await self.repository.get_channel(-1008))["chat_type"], "supergroup")
        self.assertIsNotNone(await self.repository.get_analytics_state(-1008))
        self.assertEqual(self.bot.messages[-1]["text"], "已自动绑定超级群组“Group”（ID: -1008）。")

    async def test_startup_reconciles_existing_managed_chat_identity_and_initializes_analytics(self) -> None:
        await self.repository.upsert_user(42, "Alice")
        await self.repository.upsert_channel(-1007, "Old title", "old")
        await self.repository.bind_manager(42, -1007, max_channels=1)
        self.gateway.channels[-1007] = ChannelIdentity(-1007, "Group", "group", "supergroup")

        self.assertEqual(await self.membership.reconcile_managed_chats(datetime(2026, 1, 2, tzinfo=UTC)), 1)

        chat = await self.repository.get_channel(-1007)
        self.assertEqual((chat["title"], chat["username"], chat["chat_type"]), ("Group", "group", "supergroup"))
        self.assertIsNotNone(await self.repository.get_analytics_state(-1007))

    async def test_non_admin_actor_records_channel_and_audits_without_manager_or_pause(self) -> None:
        self.gateway.user_admin = False

        await self.membership.handle(channel_event())

        self.assertIsNotNone(await self.repository.get_channel(-1007))
        self.assertFalse(await self.repository.is_bound_manager(42, -1007))
        audit = (await self.audit_rows("channel.auto_bind_failed"))[0]
        self.assertEqual((audit["actor_user_id"], audit["details"]["error_type"]), (42, "PermissionDenied"))
        self.assertEqual((await self.repository.get_channel(-1007))["status"], "active")

    async def test_missing_bot_capabilities_keeps_record_and_audits_failed_binding(self) -> None:
        self.gateway.bot_ready = False

        await self.membership.handle(channel_event())

        self.assertIsNotNone(await self.repository.get_channel(-1007))
        self.assertFalse(await self.repository.is_bound_manager(42, -1007))
        audit = (await self.audit_rows("channel.auto_bind_failed"))[0]
        self.assertEqual(audit["details"]["error_type"], "PermissionDenied")
        self.assertIn("机器人", audit["details"]["message"])

    async def test_channel_quota_retains_discovered_channel_without_manager(self) -> None:
        await self.repository.upsert_user(42, "Alice")
        await self.repository.upsert_channel(-1008, "Other", "other")
        await self.repository.bind_manager(42, -1008, max_channels=1)

        await self.membership.handle(channel_event())

        self.assertIsNotNone(await self.repository.get_channel(-1007))
        self.assertFalse(await self.repository.is_bound_manager(42, -1007))
        self.assertEqual((await self.audit_rows("channel.auto_bind_failed"))[0]["details"]["error_type"], "ResourceLimitError")

    async def test_binding_gateway_network_failure_is_contained_and_audited_once(self) -> None:
        self.gateway.user_error = TelegramNetworkError(GetMe(), "network unavailable")

        await self.membership.handle(channel_event())

        channel = await self.repository.get_channel(-1007)
        self.assertEqual((channel["status"], channel["enabled"]), ("active", 1))
        self.assertFalse(await self.repository.is_bound_manager(42, -1007))
        failures = await self.audit_rows("channel.auto_bind_failed")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["details"]["error_type"], "TelegramNetworkError")

    async def test_permission_unavailable_audit_preserves_underlying_lookup_type_and_message(self) -> None:
        class ForbiddenLookupBot:
            async def get_chat(self, reference):
                return SimpleNamespace(id=-1007, title="News", username="news", type="channel")

            async def get_chat_member(self, chat_id, user_id):
                raise TelegramForbiddenError(GetMe(), "bot was kicked during lookup")

        permission_gateway = BotApiPermissionGateway(ForbiddenLookupBot())
        channels = ChannelService(
            self.repository,
            PermissionService(self.repository, permission_gateway),
            max_channels=1,
            max_slots=10,
            storage_channel_id=-10050,
        )
        membership = ChatMembershipService(
            self.repository,
            channels,
            self.notifier,
            storage_channel_id=-10050,
        )

        await membership.handle(channel_event())

        failures = await self.audit_rows("channel.auto_bind_failed")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["details"]["error_type"], "PermissionUnavailable")
        self.assertIn("TelegramForbiddenError", failures[0]["details"]["message"])
        self.assertIn("bot was kicked during lookup", failures[0]["details"]["message"])

    async def test_capability_recheck_retry_failure_is_contained_without_pause_or_second_audit(self) -> None:
        await self.repository.upsert_user(1, "Manager")
        await self.repository.upsert_channel(-1007, "News", "news")
        await self.repository.bind_manager(1, -1007, max_channels=1)
        self.gateway.bot_capability_outcomes = [
            BotCapabilities(False, False, False),
            TelegramRetryAfter(GetMe(), "slow down", retry_after=2),
        ]

        await self.membership.handle(channel_event())

        channel = await self.repository.get_channel(-1007)
        self.assertEqual((channel["status"], channel["enabled"]), ("active", 1))
        self.assertTrue(await self.repository.is_bound_manager(1, -1007))
        self.assertFalse(await self.repository.is_bound_manager(42, -1007))
        failures = await self.audit_rows("channel.auto_bind_failed")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["details"]["error_type"], "PermissionDenied")

    async def test_existing_configuration_pauses_only_when_bot_capabilities_are_lost(self) -> None:
        await self.repository.upsert_user(1, "Manager")
        await self.repository.upsert_channel(-1007, "News", "news")
        await self.repository.bind_manager(1, -1007, max_channels=1)
        self.gateway.user_admin = False
        self.gateway.bot_ready = False

        await self.membership.handle(channel_event())

        channel = await self.repository.get_channel(-1007)
        self.assertEqual((channel["status"], channel["enabled"]), ("paused", 0))
        self.assertTrue(await self.repository.is_bound_manager(1, -1007))

    async def test_invalid_actor_does_not_pause_an_existing_configuration(self) -> None:
        await self.repository.upsert_user(1, "Manager")
        await self.repository.upsert_channel(-1007, "News", "news")
        await self.repository.bind_manager(1, -1007, max_channels=1)
        self.gateway.user_admin = False

        await self.membership.handle(channel_event())

        channel = await self.repository.get_channel(-1007)
        self.assertEqual((channel["status"], channel["enabled"]), ("active", 1))
        self.assertTrue(await self.repository.is_bound_manager(1, -1007))

    async def test_private_notification_failure_does_not_roll_back_auto_binding(self) -> None:
        self.bot.error = TelegramForbiddenError(GetMe(), "bot was blocked by the user")

        await self.membership.handle(channel_event())

        self.assertTrue(await self.repository.is_bound_manager(42, -1007))
        self.assertEqual(len(await self.audit_rows("channel.auto_bind")), 1)

    async def test_access_loss_pauses_but_preserves_slots_and_managers(self) -> None:
        await self.repository.upsert_user(1, "Manager")
        await self.repository.upsert_channel(-1007, "News", "news")
        await self.repository.bind_manager(1, -1007, max_channels=1)
        draft = await self.repository.create_draft(1, "Post", (ContentItem(text="body"),), (), 50)
        await self.repository.assign_slot(-1007, 1, draft.current_revision.id, 1, 10, "Post")

        await self.membership.handle(channel_event(actor_id=99, old_status="administrator", new_status="left"))

        channel = await self.repository.get_channel(-1007)
        self.assertEqual((channel["status"], channel["enabled"]), ("paused", 0))
        self.assertTrue(await self.repository.is_bound_manager(1, -1007))
        self.assertEqual(len(await self.repository.list_channel_slots(-1007)), 1)
        audit = (await self.audit_rows("channel.bot_access_lost"))[0]
        self.assertEqual((audit["actor_user_id"], audit["details"]), (99, {"old_status": "administrator", "new_status": "left"}))
        self.assertFalse((await self.repository.get_daily_member_stats(-1007, "2026-01-02")).is_complete)

    async def test_unknown_channel_access_loss_is_ignored(self) -> None:
        await self.membership.handle(channel_event(channel_id=-1008, old_status="administrator", new_status="kicked"))

        self.assertEqual(await self.audit_rows("channel.bot_access_lost"), [])

    async def test_access_loss_statuses_accept_enum_like_values(self) -> None:
        class Status:
            value = "restricted"

        await self.repository.upsert_channel(-1007, "News", "news")
        await self.membership.handle(channel_event(old_status="administrator", new_status=Status()))

        self.assertEqual((await self.repository.get_channel(-1007))["status"], "paused")


class TelegramAdminNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_notify_user_returns_false_for_telegram_private_message_failure(self) -> None:
        bot = FakeBot()
        bot.error = TelegramForbiddenError(GetMe(), "bot was blocked by the user")
        notifier = TelegramAdminNotifier(bot, SimpleNamespace(), SimpleNamespace())

        self.assertFalse(await notifier.notify_user(42, "hello"))

    async def test_bot_removal_during_admin_lookup_preserves_manager_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database = await Database.open(Path(tempdir) / "test.sqlite3")
            try:
                repository = Repository(database)
                await repository.upsert_user(42, "Alice")
                await repository.upsert_channel(-1007, "News", "news")
                await repository.bind_manager(42, -1007, max_channels=10)

                class RemovedBot(FakeBot):
                    async def get_chat_member(self, chat_id, user_id):
                        raise TelegramForbiddenError(GetMe(), "bot was kicked")

                bot = RemovedBot()
                notifier = TelegramAdminNotifier(bot, repository, BotApiPermissionGateway(bot))

                await notifier.notify_channel_admins(-1007, "发布失败")

                self.assertTrue(await repository.is_bound_manager(42, -1007))
                self.assertEqual(bot.messages, [])
            finally:
                await database.close()


class AppRegistrationTests(unittest.TestCase):
    def test_member_observers_are_included_in_allowed_updates(self) -> None:
        async def handle(*args):
            return None

        dispatcher = Dispatcher()
        dispatcher.include_router(
            build_router(
                SimpleNamespace(on_private_message=handle, on_callback=handle),
                SimpleNamespace(handle=handle),
                SimpleNamespace(handle=handle),
                SimpleNamespace(handle=handle),
            )
        )

        self.assertIn("my_chat_member", dispatcher.resolve_used_update_types())
        self.assertIn("chat_member", dispatcher.resolve_used_update_types())


class MemberUpdateAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_aiogram_update_id_to_analytics_service(self) -> None:
        calls = []

        class Analytics:
            async def record_member_update(self, update_id, event):
                calls.append((update_id, event))

        event = channel_event()
        await MemberUpdateAdapter(Analytics()).handle(event, SimpleNamespace(update_id=123))

        self.assertEqual(calls, [(123, event)])


if __name__ == "__main__":
    unittest.main()
