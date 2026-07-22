import os
from datetime import time
import unittest
from unittest.mock import patch

from bottom_post_bot import config
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
        self.assertEqual(settings.stats_timezone, "Asia/Shanghai")
        self.assertEqual(settings.stats_push_time, time(0, 5))

    def test_loads_valid_analytics_timezone_and_push_time(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "123:token",
            "STORAGE_CHANNEL_ID": "-1001234567890",
            "OPERATOR_USER_IDS": "7",
            "STATS_TIMEZONE": "Europe/London",
            "STATS_PUSH_TIME": "23:59",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.stats_timezone, "Europe/London")
        self.assertEqual(settings.stats_push_time, time(23, 59))

    def test_rejects_invalid_analytics_timezone_and_push_time(self) -> None:
        base = {
            "TELEGRAM_BOT_TOKEN": "123:token",
            "STORAGE_CHANNEL_ID": "-1001234567890",
            "OPERATOR_USER_IDS": "7",
        }
        for name, value in (("STATS_TIMEZONE", "Mars/Olympus"), ("STATS_PUSH_TIME", "9:05"), ("STATS_PUSH_TIME", "24:00")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, base | {name: value}, clear=True):
                with self.assertRaisesRegex(ConfigurationError, name):
                    Settings.from_env()

    def test_wraps_malformed_timezone_key_as_configuration_error(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "STORAGE_CHANNEL_ID": "-1001",
            "OPERATOR_USER_IDS": "7",
            "STATS_TIMEZONE": "Asia/Shanghai",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(config, "ZoneInfo", side_effect=ValueError("bad key")):
            with self.assertRaisesRegex(ConfigurationError, "STATS_TIMEZONE"):
                Settings.from_env()

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
