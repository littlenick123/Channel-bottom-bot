from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when environment configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _integer(name: str, default: int | None = None, *, positive: bool = False) -> int:
    raw = os.getenv(name)
    if raw is None and default is not None:
        value = default
    else:
        try:
            value = int(_required(name) if raw is None else raw)
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an integer") from exc
    if positive and value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    storage_channel_id: int
    operator_user_ids: frozenset[int]
    database_path: Path = Path("data/bot.sqlite3")
    refresh_delay_seconds: int = 10
    max_channels_per_user: int = 10
    max_drafts_per_user: int = 50
    max_slots_per_channel: int = 10
    log_level: str = "INFO"
    conversation_timeout_seconds: int = 900
    pending_draft_ttl_seconds: int = 600
    pending_cleanup_interval_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = _required("TELEGRAM_BOT_TOKEN")
        storage_channel_id = _integer("STORAGE_CHANNEL_ID")
        operators_raw = _required("OPERATOR_USER_IDS")
        try:
            operators = frozenset(int(value.strip()) for value in operators_raw.split(",") if value.strip())
        except ValueError as exc:
            raise ConfigurationError("OPERATOR_USER_IDS must be comma-separated integers") from exc
        if not operators:
            raise ConfigurationError("OPERATOR_USER_IDS must contain at least one user ID")
        return cls(
            bot_token=bot_token,
            storage_channel_id=storage_channel_id,
            operator_user_ids=operators,
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            refresh_delay_seconds=_integer("REFRESH_DELAY_SECONDS", 10, positive=True),
            max_channels_per_user=_integer("MAX_CHANNELS_PER_USER", 10, positive=True),
            max_drafts_per_user=_integer("MAX_DRAFTS_PER_USER", 50, positive=True),
            max_slots_per_channel=_integer("MAX_SLOTS_PER_CHANNEL", 10, positive=True),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            conversation_timeout_seconds=_integer("CONVERSATION_TIMEOUT_SECONDS", 900, positive=True),
            pending_draft_ttl_seconds=_integer("PENDING_DRAFT_TTL_SECONDS", 600, positive=True),
            pending_cleanup_interval_seconds=_integer("PENDING_CLEANUP_INTERVAL_SECONDS", 60, positive=True),
        )
