import unittest

from bottom_post_bot.domain import ContentItem, DraftRevision, PublishedMessageRef, SlotSnapshot
from bottom_post_bot.publisher import Publisher, RefreshOutcome


class FakeGateway:
    def __init__(self, *, fail_on_send: int | None = None) -> None:
        self.fail_on_send = fail_on_send
        self.sent: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, ...]] = []
        self.group_calls = []

    async def delete_messages(self, channel_id: int, message_ids: list[int]) -> None:
        self.deleted.append(tuple(message_ids))

    async def send_content(self, channel_id: int, item: ContentItem, buttons, silent: bool) -> list[PublishedMessageRef]:
        call_number = len(self.sent) + 1
        if self.fail_on_send == call_number:
            raise OSError("network down")
        message_id = 100 + call_number
        self.sent.append((message_id, item.text or ""))
        return [PublishedMessageRef(message_id)]

    async def send_content_group(self, channel_id: int, items, buttons, silent: bool) -> list[PublishedMessageRef]:
        self.group_calls.append(tuple(items))
        ids = []
        for item in items:
            message_id = 100 + len(self.sent) + 1
            self.sent.append((message_id, item.text or ""))
            ids.append(message_id)
        return [PublishedMessageRef(message_id) for message_id in ids]


class FakeState:
    def __init__(self, slots: list[SlotSnapshot], previous: list[int] | None = None) -> None:
        self.slots = slots
        self.previous = previous or []
        self.saved: list[int] | None = None
        self.failed = False
        self.recorded: list[int] = []
        self.pending: list[PublishedMessageRef] = []
        self.acknowledged: list[int] = []
        self.batch_failed = False

    async def load_publish_state(self, channel_id: int):
        return self.slots, self.previous, True

    async def commit_batch(self, channel_id: int, message_ids: list[int]) -> None:
        self.saved = message_ids

    async def begin_batch(self, channel_id: int) -> int:
        return 55

    async def record_batch_results(self, batch_id: int, messages: list[PublishedMessageRef]) -> None:
        self.recorded.extend(message.message_id for message in messages if message.message_id > 0)
        self.pending.extend(message for message in messages if message.message_id == 0)

    async def acknowledge_deleted_messages(self, channel_id: int, message_ids: list[int]) -> None:
        self.acknowledged.extend(message_ids)

    async def finalize_batch(self, channel_id: int, batch_id: int) -> None:
        self.saved = list(self.recorded)

    async def fail_batch(self, batch_id: int, error: str, *, needs_cleanup: bool) -> None:
        self.batch_failed = True

    async def mark_failure(self, channel_id: int, error: str) -> None:
        self.failed = True


def make_slot(number: int, text: str) -> SlotSnapshot:
    revision = DraftRevision(number, 1, (ContentItem(text=text),), ())
    return SlotSnapshot(99, number, revision, enabled=True)


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_old_batch_and_sends_slots_high_to_low(self) -> None:
        gateway = FakeGateway()
        state = FakeState([make_slot(1, "one"), make_slot(3, "three")], [7, 8])

        outcome = await Publisher(gateway, state).refresh(99)

        self.assertEqual(outcome, RefreshOutcome.SUCCESS)
        self.assertEqual(gateway.deleted, [(7, 8)])
        self.assertEqual(state.acknowledged, [7, 8])
        self.assertEqual([text for _, text in gateway.sent], ["three", "one"])
        self.assertEqual(state.saved, [101, 102])
        self.assertEqual(state.recorded, [101, 102])

    async def test_scheduled_video_placeholder_is_recorded_without_trying_to_delete_id_zero(self) -> None:
        gateway = FakeGateway()

        async def send_scheduled_video(channel_id, item, buttons, silent):
            gateway.sent.append((0, item.text or ""))
            return [PublishedMessageRef(0, "video-fingerprint")]

        gateway.send_content = send_scheduled_video
        state = FakeState([make_slot(1, "scheduled video")], [7])

        outcome = await Publisher(gateway, state).refresh(99)

        self.assertEqual(outcome, RefreshOutcome.SUCCESS)
        self.assertEqual(gateway.deleted, [(7,)])
        self.assertEqual(state.recorded, [])
        self.assertEqual(state.pending, [PublishedMessageRef(0, "video-fingerprint")])
        self.assertEqual(state.saved, [])

    async def test_cleans_partial_new_batch_when_sending_fails(self) -> None:
        gateway = FakeGateway(fail_on_send=2)
        state = FakeState([make_slot(1, "one"), make_slot(2, "two")])

        outcome = await Publisher(gateway, state).refresh(99)

        self.assertEqual(outcome, RefreshOutcome.RETRY)
        self.assertEqual(gateway.deleted[-1], (101,))
        self.assertTrue(state.failed)
        self.assertTrue(state.batch_failed)
        self.assertIsNone(state.saved)

    async def test_album_items_are_sent_as_one_group(self) -> None:
        gateway = FakeGateway()
        revision = DraftRevision(
            1,
            1,
            (
                ContentItem(text="caption", storage_message_id=1, grouped_id="album"),
                ContentItem(storage_message_id=2, grouped_id="album"),
            ),
            (),
        )
        state = FakeState([SlotSnapshot(99, 1, revision, enabled=True)])

        outcome = await Publisher(gateway, state).refresh(99)

        self.assertEqual(outcome, RefreshOutcome.SUCCESS)
        self.assertEqual(len(gateway.group_calls), 1)
        self.assertEqual(state.saved, [101, 102])


if __name__ == "__main__":
    unittest.main()
