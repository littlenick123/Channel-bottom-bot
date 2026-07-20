import os
import unittest
from unittest.mock import patch

from bottom_post_bot.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_loads_required_values_and_defaults(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "123:token",
            "STORAGE_CHANNEL_ID": "-1001234567890",
            "OPERATOR_USER_IDS": "7, 8",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.bot_token, "123:token")
        self.assertEqual(settings.storage_channel_id, -1001234567890)
        self.assertEqual(settings.operator_user_ids, frozenset({7, 8}))
        self.assertEqual(settings.refresh_delay_seconds, 10)
        self.assertEqual(settings.max_channels_per_user, 10)
        self.assertEqual(settings.max_drafts_per_user, 50)
        self.assertEqual(settings.max_slots_per_channel, 10)
        self.assertEqual(settings.pending_draft_ttl_seconds, 600)
        self.assertEqual(settings.pending_cleanup_interval_seconds, 60)

    def test_rejects_missing_required_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "TELEGRAM_BOT_TOKEN"):
                Settings.from_env()

    def test_rejects_non_positive_limits(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "STORAGE_CHANNEL_ID": "-1001",
            "OPERATOR_USER_IDS": "7",
            "MAX_SLOTS_PER_CHANNEL": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "MAX_SLOTS_PER_CHANNEL"):
                Settings.from_env()

    def test_rejects_non_positive_pending_draft_settings(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "STORAGE_CHANNEL_ID": "-1001",
            "OPERATOR_USER_IDS": "7",
            "PENDING_DRAFT_TTL_SECONDS": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "PENDING_DRAFT_TTL_SECONDS"):
                Settings.from_env()
