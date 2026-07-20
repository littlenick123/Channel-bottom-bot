import tempfile
import unittest
from pathlib import Path

from bottom_post_bot.channels import ChannelIdentity, ChannelService
from bottom_post_bot.database import Database
from bottom_post_bot.domain import ContentItem
from bottom_post_bot.permissions import BotCapabilities, PermissionDenied, PermissionService
from bottom_post_bot.repositories import Repository


class FakePermissionGateway:
    def __init__(self, *, user_admin: bool = True, bot_ok: bool = True) -> None:
        self.user_admin = user_admin
        self.bot_ok = bot_ok
        self.resolve_calls: list[str | int] = []

    async def resolve_channel(self, reference):
        self.resolve_calls.append(reference)
        return ChannelIdentity(-1007, "News", "news")

    async def user_is_admin(self, channel_id: int, user_id: int) -> bool:
        return self.user_admin

    async def bot_capabilities(self, channel_id: int) -> BotCapabilities:
        return BotCapabilities(is_admin=self.bot_ok, can_post=self.bot_ok, can_delete=self.bot_ok)


class PermissionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = await Database.open(Path(self.tempdir.name) / "test.sqlite3")
        self.repo = Repository(self.db)
        await self.repo.upsert_user(1, "Alice")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_binding_requires_current_channel_admin(self) -> None:
        permissions = PermissionService(self.repo, FakePermissionGateway(user_admin=False))
        with self.assertRaisesRegex(PermissionDenied, "管理员"):
            await permissions.assert_can_bind(1, "@news")

    async def test_binding_requires_bot_post_and_delete_permissions(self) -> None:
        permissions = PermissionService(self.repo, FakePermissionGateway(bot_ok=False))
        with self.assertRaisesRegex(PermissionDenied, "机器人"):
            await permissions.assert_can_bind(1, "@news")

    async def test_each_management_action_rechecks_live_permission(self) -> None:
        gateway = FakePermissionGateway()
        permissions = PermissionService(self.repo, gateway)
        channel = ChannelService(
            self.repo,
            permissions,
            max_channels=10,
            max_slots=10,
            storage_channel_id=-10050,
        )
        await channel.bind(1, "@news")
        gateway.user_admin = False

        with self.assertRaises(PermissionDenied):
            await permissions.assert_user_can_manage(1, -1007)
        self.assertFalse(await self.repo.is_bound_manager(1, -1007))

    async def test_new_channel_uses_configured_default_refresh_delay(self) -> None:
        permissions = PermissionService(self.repo, FakePermissionGateway())
        channel = ChannelService(
            self.repo,
            permissions,
            max_channels=10,
            max_slots=10,
            storage_channel_id=-10050,
            default_refresh_delay=17,
        )
        await channel.bind(1, "@news")
        stored = await self.repo.get_channel(-1007)
        self.assertEqual(stored["refresh_delay_seconds"], 17)

    async def test_assign_slot_accepts_only_owned_draft(self) -> None:
        await self.repo.upsert_user(2, "Bob")
        permissions = PermissionService(self.repo, FakePermissionGateway())
        channels = ChannelService(
            self.repo,
            permissions,
            max_channels=10,
            max_slots=10,
            storage_channel_id=-10050,
        )
        await channels.bind(1, "@news")
        draft = await self.repo.create_draft(2, "Bob", (ContentItem(text="secret"),), (), max_drafts=50)

        with self.assertRaises(PermissionDenied):
            await channels.assign_slot(-1007, 1, draft.current_revision.id, actor_id=1)

    async def test_move_slot_swaps_with_occupied_target(self) -> None:
        permissions = PermissionService(self.repo, FakePermissionGateway())
        channels = ChannelService(
            self.repo,
            permissions,
            max_channels=10,
            max_slots=10,
            storage_channel_id=-10050,
        )
        await channels.bind(1, "@news")
        first = await self.repo.create_draft(1, "one", (ContentItem(text="one"),), (), 50)
        second = await self.repo.create_draft(1, "two", (ContentItem(text="two"),), (), 50)
        await channels.assign_slot(-1007, 1, first.current_revision.id, 1)
        await channels.assign_slot(-1007, 2, second.current_revision.id, 1)
        await channels.set_slot_enabled(-1007, 1, False, 1)
        await channels.rename_slot(-1007, 2, "首页入口", 1)

        await channels.move_slot(-1007, 1, 2, 1)

        slots = await self.repo.list_channel_slots(-1007)
        self.assertEqual(
            [
                (slot.slot_number, slot.revision.id, slot.revision.items[0].text, slot.display_name, slot.name_customized, slot.enabled)
                for slot in slots
            ],
            [
                (1, second.current_revision.id, "two", "首页入口", True, True),
                (2, first.current_revision.id, "one", "one", False, False),
            ],
        )

    async def test_assignment_updates_uncustomized_name_and_preserves_custom_name_until_clear(self) -> None:
        permissions = PermissionService(self.repo, FakePermissionGateway())
        channels = ChannelService(
            self.repo,
            permissions,
            max_channels=10,
            max_slots=10,
            storage_channel_id=-10050,
        )
        await channels.bind(1, "@news")
        first = await self.repo.create_draft(1, "First", (ContentItem(text="one"),), (), 50)
        second = await self.repo.create_draft(1, "Second", (ContentItem(text="two"),), (), 50)

        await channels.assign_slot(-1007, 1, first.current_revision.id, 1)
        self.assertEqual((await self.repo.list_channel_slots(-1007))[0].display_name, "First")
        await channels.assign_slot(-1007, 1, second.current_revision.id, 1)
        self.assertEqual((await self.repo.list_channel_slots(-1007))[0].display_name, "Second")

        await channels.rename_slot(-1007, 1, " 首页入口 ", 1)
        await channels.assign_slot(-1007, 1, first.current_revision.id, 1)
        slot = (await self.repo.list_channel_slots(-1007))[0]
        self.assertEqual((slot.display_name, slot.name_customized), ("首页入口", True))

        await channels.clear_slot(-1007, 1, 1)
        self.assertEqual(await self.repo.list_channel_slots(-1007), [])

    async def test_move_slot_to_empty_target_carries_the_full_slot_row(self) -> None:
        permissions = PermissionService(self.repo, FakePermissionGateway())
        channels = ChannelService(
            self.repo,
            permissions,
            max_channels=10,
            max_slots=10,
            storage_channel_id=-10050,
        )
        await channels.bind(1, "@news")
        first = await self.repo.create_draft(1, "First", (ContentItem(text="one"),), (), 50)
        second = await self.repo.create_draft(1, "Second", (ContentItem(text="two"),), (), 50)
        await channels.assign_slot(-1007, 1, first.current_revision.id, 1)
        await channels.rename_slot(-1007, 1, "首页入口", 1)
        await channels.set_slot_enabled(-1007, 1, False, 1)

        await channels.move_slot(-1007, 1, 3, 1)

        slots = await self.repo.list_channel_slots(-1007)
        self.assertEqual(
            [
                (slot.slot_number, slot.revision.id, slot.revision.items[0].text, slot.display_name, slot.name_customized, slot.enabled)
                for slot in slots
            ],
            [(3, first.current_revision.id, "one", "首页入口", True, False)],
        )

    async def test_manual_binding_rejects_storage_channel_before_lookup_or_persistence(self) -> None:
        gateway = FakePermissionGateway()
        permissions = PermissionService(self.repo, gateway)
        channels = ChannelService(
            self.repo,
            permissions,
            max_channels=10,
            max_slots=10,
            storage_channel_id=-10050,
        )

        with self.assertRaisesRegex(PermissionDenied, "存储频道"):
            await channels.bind(1, -10050)

        self.assertEqual(gateway.resolve_calls, [])
        self.assertIsNone(await self.repo.get_channel(-10050))
        self.assertFalse(await self.repo.is_bound_manager(1, -10050))
        self.assertEqual(
            await self.db.fetch_value("SELECT COUNT(*) FROM audit_logs WHERE channel_id=?", (-10050,)),
            0,
        )


if __name__ == "__main__":
    unittest.main()
