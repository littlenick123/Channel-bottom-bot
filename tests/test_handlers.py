import unittest
from types import SimpleNamespace

from bottom_post_bot.domain import ButtonSpec
from bottom_post_bot.handlers import parse_button_batch, parse_button_input
from bottom_post_bot.listeners import ChannelListener


class FakeRepository:
    def __init__(self, managed=True, current=False, delay=10) -> None:
        self.managed = managed
        self.current = current
        self.delay = delay

    async def channel_refresh_delay(self, channel_id):
        return self.delay if self.managed else None

    async def is_current_sent_message(self, channel_id, message_id):
        return self.current


class FakeScheduler:
    def __init__(self) -> None:
        self.calls = []

    async def request(self, channel_id, reason, delay_seconds):
        self.calls.append((channel_id, reason, delay_seconds))


class HandlerHelpersTests(unittest.TestCase):
    def test_parse_button_input_uses_one_based_row_for_users(self) -> None:
        button = parse_button_input("官网 | https://example.com | 2")
        self.assertEqual(button.text, "官网")
        self.assertEqual(button.row, 1)
        self.assertEqual(button.column, 0)

    def test_parse_button_input_rejects_invalid_format(self) -> None:
        with self.assertRaises(ValueError):
            parse_button_input("only text")
        with self.assertRaisesRegex(ValueError, "行号"):
            parse_button_input("官网 | https://example.com")

    def test_parse_button_batch_appends_each_line_to_its_requested_row(self) -> None:
        existing = (ButtonSpec("旧按钮", "https://old.example", 0, 0),)

        buttons = parse_button_batch(
            "官网 | https://example.com | 1\n客服 | tg://resolve?domain=example | 1\n下载 | https://example.com/d | 2",
            existing,
        )

        self.assertEqual([(button.row, button.column) for button in buttons], [(0, 0), (0, 1), (0, 2), (1, 0)])

    def test_parse_button_batch_ignores_blank_lines_but_reports_physical_line_numbers(self) -> None:
        buttons = parse_button_batch("官网 | https://example.com | 1\n\n客服 | https://support.example | 1")
        self.assertEqual([(button.row, button.column) for button in buttons], [(0, 0), (0, 1)])

        with self.assertRaisesRegex(ValueError, "第 3 行"):
            parse_button_batch("官网 | https://example.com | 1\n\nbroken")

    def test_parse_button_batch_identifies_malformed_and_invalid_url_lines(self) -> None:
        with self.assertRaisesRegex(ValueError, "第 2 行"):
            parse_button_batch("官网 | https://example.com | 1\nbroken")
        with self.assertRaisesRegex(ValueError, "第 2 行"):
            parse_button_batch("官网 | https://example.com | 1\n客服 | invalid-url | 1")

    def test_parse_button_batch_enforces_strict_url_prefixes_and_normalizes_valid_url(self) -> None:
        buttons = parse_button_batch("官网 |  HTTPS://Example.com/path  | 1")
        self.assertEqual(buttons[0].url, "HTTPS://Example.com/path")

        for url in ("https:foo", "http:foo", "tg:foo", "https://", "http://", "ftp://example.com"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "第 2 行"):
                parse_button_batch(f"官网 | https://example.com | 1\n坏链接 |  {url}  | 1")

    def test_parse_button_batch_rejects_ninth_button_in_a_row(self) -> None:
        value = "\n".join(f"按钮{number} | https://example.com/{number} | 1" for number in range(1, 10))

        with self.assertRaisesRegex(ValueError, "第 9 行.*8"):
            parse_button_batch(value)

    def test_parse_button_batch_rejects_one_hundred_and_first_button(self) -> None:
        value = "\n".join(f"按钮{number} | https://example.com/{number} | {number}" for number in range(1, 102))

        with self.assertRaisesRegex(ValueError, "第 101 行.*100"):
            parse_button_batch(value)


class ChannelListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedules_known_external_channel_message(self) -> None:
        repo = FakeRepository()
        scheduler = FakeScheduler()
        listener = ChannelListener(repo, scheduler)
        event = SimpleNamespace(chat_id=-1007, id=12, out=False, message=SimpleNamespace(action=None))
        await listener.handle(event)
        self.assertEqual(scheduler.calls, [(-1007, "channel-message:12", 10)])

    async def test_ignores_current_bot_batch_message(self) -> None:
        repo = FakeRepository(current=True)
        scheduler = FakeScheduler()
        listener = ChannelListener(repo, scheduler)
        event = SimpleNamespace(chat_id=-1007, id=12, out=False, message=SimpleNamespace(action=None))
        await listener.handle(event)
        self.assertEqual(scheduler.calls, [])

    async def test_ignores_outgoing_and_unmanaged_messages(self) -> None:
        scheduler = FakeScheduler()
        listener = ChannelListener(FakeRepository(managed=False), scheduler)
        await listener.handle(SimpleNamespace(chat_id=-1007, id=12, out=True, message=SimpleNamespace(action=None)))
        self.assertEqual(scheduler.calls, [])

    async def test_accepts_aiogram_channel_post_shape(self) -> None:
        scheduler = FakeScheduler()
        listener = ChannelListener(FakeRepository(), scheduler)
        message = SimpleNamespace(chat=SimpleNamespace(id=-1007), message_id=22, content_type="text")
        await listener.handle(message)
        self.assertEqual(scheduler.calls, [(-1007, "channel-message:22", 10)])

    async def test_schedules_external_supergroup_message_but_filters_bot_and_service_messages(self) -> None:
        scheduler = FakeScheduler()
        listener = ChannelListener(FakeRepository(), scheduler)
        base = dict(chat=SimpleNamespace(id=-1007), message_id=22)
        await listener.handle(SimpleNamespace(**base, from_user=SimpleNamespace(is_bot=False), content_type="text"))
        await listener.handle(SimpleNamespace(**base, from_user=SimpleNamespace(is_bot=True)))
        await listener.handle(
            SimpleNamespace(**base, new_chat_members=[SimpleNamespace(id=1)], content_type="new_chat_members")
        )

        self.assertEqual(scheduler.calls, [(-1007, "channel-message:22", 10)])

    async def test_rejects_every_representative_aiogram_service_content_type(self) -> None:
        scheduler = FakeScheduler()
        listener = ChannelListener(FakeRepository(), scheduler)
        for content_type in (
            "forum_topic_created",
            "video_chat_started",
            "message_auto_delete_timer_changed",
            "boost_added",
            "general_forum_topic_hidden",
            "suggested_post_paid",
        ):
            with self.subTest(content_type=content_type):
                await listener.handle(
                    SimpleNamespace(
                        chat=SimpleNamespace(id=-1007),
                        message_id=22,
                        from_user=SimpleNamespace(is_bot=False),
                        content_type=content_type,
                    )
                )

        self.assertEqual(scheduler.calls, [])


if __name__ == "__main__":
    unittest.main()
