import os
from datetime import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from bottom_post_bot import app, config
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

    def test_timezone_error_explains_missing_tzdata_dependency(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "STORAGE_CHANNEL_ID": "-1001",
            "OPERATOR_USER_IDS": "7",
            "STATS_TIMEZONE": "Asia/Shanghai",
        }
        missing = ZoneInfoNotFoundError("No time zone found with key Asia/Shanghai")
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(config, "ZoneInfo", side_effect=missing),
            patch.object(config, "find_spec", return_value=None),
        ):
            with self.assertRaisesRegex(ConfigurationError, "tzdata"):
                Settings.from_env()

    def test_invalid_timezone_does_not_blame_tzdata_when_package_is_installed(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "STORAGE_CHANNEL_ID": "-1001",
            "OPERATOR_USER_IDS": "7",
            "STATS_TIMEZONE": "Mars/Olympus",
        }
        unknown = ZoneInfoNotFoundError("No time zone found with key Mars/Olympus")
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(config, "ZoneInfo", side_effect=unknown),
            patch("bottom_post_bot.config.find_spec", create=True, return_value=object()),
        ):
            with self.assertRaises(ConfigurationError) as raised:
                Settings.from_env()

        self.assertNotIn("install project dependencies", str(raised.exception))

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


class StartupEnvironmentTests(unittest.TestCase):
    def test_main_loads_dotenv_before_reading_settings(self) -> None:
        order: list[str] = []

        def load_settings():
            self.assertEqual(order, ["dotenv"])
            order.append("settings")
            return SimpleNamespace(log_level="INFO")

        def run_coroutine(coroutine):
            coroutine.close()

        with (
            patch("bottom_post_bot.app.load_dotenv", create=True, side_effect=lambda: order.append("dotenv")),
            patch.object(app.Settings, "from_env", side_effect=load_settings),
            patch.object(app, "configure_logging"),
            patch.object(app.asyncio, "run", side_effect=run_coroutine),
        ):
            app.main()

        self.assertEqual(order, ["dotenv", "settings"])

    def test_main_renders_storage_channel_startup_error_without_raw_exception(self) -> None:
        startup_error = getattr(app, "StorageChannelAccessError", RuntimeError)

        def fail_startup(coroutine):
            coroutine.close()
            raise startup_error("storage unavailable")

        with (
            patch.object(app, "load_dotenv"),
            patch.object(app.Settings, "from_env", return_value=SimpleNamespace(log_level="INFO")),
            patch.object(app, "configure_logging"),
            patch.object(app.asyncio, "run", side_effect=fail_startup),
        ):
            with self.assertRaisesRegex(SystemExit, "Startup error: storage unavailable"):
                app.main()
