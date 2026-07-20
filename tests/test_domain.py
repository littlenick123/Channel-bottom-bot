import unittest

from bottom_post_bot import domain
from bottom_post_bot.domain import (
    ButtonSpec,
    ContentItem,
    DraftRevision,
    SlotSnapshot,
    ValidationError,
    enabled_slots_in_publish_order,
)


class ButtonSpecTests(unittest.TestCase):
    def test_accepts_supported_url_schemes(self) -> None:
        for url in ("https://example.com", "http://example.com", "tg://resolve?domain=x"):
            self.assertEqual(ButtonSpec("打开", url, row=0, column=0).url, url)

    def test_rejects_unsafe_url_scheme(self) -> None:
        with self.assertRaises(ValidationError):
            ButtonSpec("危险", "javascript:alert(1)", row=0, column=0)

    def test_accepts_case_insensitive_prefix_and_stores_trimmed_url(self) -> None:
        button = ButtonSpec("打开", "  HTTPS://Example.com/path  ", row=0, column=0)

        self.assertEqual(button.url, "HTTPS://Example.com/path")

    def test_rejects_opaque_or_authorityless_supported_schemes(self) -> None:
        for url in (
            "https:foo",
            "http:foo",
            "tg:foo",
            "https://",
            "http://",
            "  https:foo  ",
            "ftp://example.com",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValidationError, "https://"):
                ButtonSpec("打开", url, row=0, column=0)

    def test_parser_level_url_error_uses_standard_validation_error(self) -> None:
        with self.assertRaisesRegex(ValidationError, "button URL must use https://"):
            ButtonSpec("打开", "https://[", row=0, column=0)

    def test_rejects_more_than_eight_buttons_in_a_row(self) -> None:
        buttons = tuple(ButtonSpec(str(i), f"https://example.com/{i}", 0, i) for i in range(9))
        with self.assertRaisesRegex(ValidationError, "8"):
            DraftRevision(1, 1, (ContentItem(text="hello"),), buttons)


class SlotOrderingTests(unittest.TestCase):
    def test_slot_can_store_a_custom_display_name(self) -> None:
        revision = DraftRevision(1, 1, (ContentItem(text="post"),))
        slot = SlotSnapshot(-1001, 2, revision, True, "活动入口", True)

        self.assertEqual((slot.display_name, slot.name_customized), ("活动入口", True))

    def test_enabled_non_empty_slots_publish_from_high_to_low(self) -> None:
        revision = DraftRevision(1, 1, (ContentItem(text="hello"),), ())
        slots = [
            SlotSnapshot(1, 1, revision, enabled=True),
            SlotSnapshot(1, 3, revision, enabled=True),
            SlotSnapshot(1, 2, revision, enabled=False),
        ]
        ordered = enabled_slots_in_publish_order(slots)
        self.assertEqual([slot.slot_number for slot in ordered], [3, 1])


class PendingDraftTests(unittest.TestCase):
    def test_defaults_to_pending_status(self) -> None:
        pending_draft = domain.PendingDraft(1, 7, (ContentItem(text="post"),), 123.0)

        self.assertEqual(pending_draft.status, "pending")
