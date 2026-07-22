from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.types import BotCommand

from .aiogram_gateway import BotApiGateway, BotApiPermissionGateway
from .analytics import AnalyticsService, MemberUpdateAdapter
from .channels import ChannelService
from .config import ConfigurationError, Settings
from .database import Database
from .drafts import DraftService
from .handlers import BotHandlers
from .listeners import ChannelListener
from .maintenance import PendingCleanupLoop
from .membership import ChatMembershipService
from .notifications import TelegramAdminNotifier
from .pending_drafts import PendingDraftService
from .permissions import PermissionService
from .publisher import Publisher
from .repositories import Repository
from .scheduler import DailyStatsScheduler, RefreshScheduler


logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def build_router(
    handlers: BotHandlers, listener: ChannelListener, membership: ChatMembershipService, member_updates: MemberUpdateAdapter | None = None
) -> Router:
    router = Router(name="bottom-post-bot")
    router.message.register(handlers.on_private_message, F.chat.type == ChatType.PRIVATE)
    router.callback_query.register(handlers.on_callback)
    router.channel_post.register(listener.handle)
    router.message.register(listener.handle, F.chat.type == ChatType.SUPERGROUP)
    router.my_chat_member.register(membership.handle)
    if member_updates is not None:
        router.chat_member.register(member_updates.handle)
    return router


async def drain_dispatcher_update_tasks(dispatcher: Dispatcher) -> None:
    """Await aiogram's concurrently handled updates after polling intake stops."""
    while True:
        tracked_tasks = getattr(dispatcher, "_handle_update_tasks", None)
        if not tracked_tasks:
            return
        tasks = tuple(tracked_tasks)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        tracked_tasks.difference_update(tasks)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                logger.warning("Aiogram update task was cancelled during shutdown")
            elif isinstance(result, BaseException):
                logger.error(
                    "Aiogram update task failed during shutdown",
                    exc_info=(type(result), result, result.__traceback__),
                )


async def run(settings: Settings) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    database = await Database.open(settings.database_path)
    bot = None
    dispatcher = None
    scheduler_task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    daily_stats_task: asyncio.Task | None = None
    scheduler: RefreshScheduler | None = None
    cleanup_loop: PendingCleanupLoop | None = None
    daily_stats_scheduler: DailyStatsScheduler | None = None
    handlers: BotHandlers | None = None
    try:
        bot = Bot(token=settings.bot_token)
        dispatcher = Dispatcher()
        await bot.get_chat(settings.storage_channel_id)
        me = await bot.get_me()
        repository = Repository(database)
        recovered_batches = await repository.recover_incomplete_batches(time.time())
        if recovered_batches:
            logger.warning("Recovered interrupted publish batches", extra={"count": recovered_batches})

        telegram = BotApiGateway(bot, settings.storage_channel_id)
        analytics = AnalyticsService(repository, telegram, settings.stats_timezone)
        permission_gateway = BotApiPermissionGateway(bot, bot_id=me.id)
        permissions = PermissionService(repository, permission_gateway)
        drafts = DraftService(repository, telegram, settings.max_drafts_per_user)
        pending_drafts = PendingDraftService(
            repository,
            telegram,
            settings.max_drafts_per_user,
            settings.pending_draft_ttl_seconds,
        )
        channels = ChannelService(
            repository,
            permissions,
            max_channels=settings.max_channels_per_user,
            max_slots=settings.max_slots_per_channel,
            storage_channel_id=settings.storage_channel_id,
            default_refresh_delay=settings.refresh_delay_seconds,
            analytics=analytics,
        )
        publisher = Publisher(telegram, repository)
        notifier = TelegramAdminNotifier(bot, repository, permission_gateway)
        scheduler = RefreshScheduler(repository, publisher, notifier=notifier)
        cleanup_loop = PendingCleanupLoop(pending_drafts, settings.pending_cleanup_interval_seconds)
        handlers = BotHandlers(
            bot, repository, drafts, channels, permissions, scheduler, telegram, settings, pending_drafts, analytics=analytics
        )
        listener = ChannelListener(repository, scheduler, bot_user_id=me.id)
        membership = ChatMembershipService(
            repository, channels, notifier, storage_channel_id=settings.storage_channel_id, analytics=analytics
        )
        startup_now = datetime.now(ZoneInfo(settings.stats_timezone))
        await analytics.heartbeat(startup_now)
        await analytics.cleanup_processed_updates(startup_now)
        await membership.reconcile_managed_chats(startup_now)
        daily_stats_scheduler = DailyStatsScheduler(
            repository,
            analytics,
            permissions,
            telegram,
            timezone=settings.stats_timezone,
            push_time=settings.stats_push_time,
        )
        router = build_router(handlers, listener, membership, MemberUpdateAdapter(analytics))
        dispatcher.include_router(router)

        await bot.set_my_commands(
            [
                BotCommand(command="start", description="打开管理菜单"),
                BotCommand(command="status", description="查看个人状态"),
                BotCommand(command="cancel", description="取消当前操作"),
                BotCommand(command="help", description="查看使用帮助"),
                BotCommand(command="stats", description="查看成员统计"),
            ]
        )
        scheduler_task = asyncio.create_task(scheduler.run_forever(), name="refresh-scheduler")
        cleanup_task = asyncio.create_task(cleanup_loop.run_forever(), name="pending-cleanup")
        daily_stats_task = asyncio.create_task(daily_stats_scheduler.run_forever(), name="daily-stats-scheduler")
        logger.info("Bot started", extra={"bot_id": me.id})
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            polling_timeout=30,
            tasks_concurrency_limit=100,
            close_bot_session=False,
        )
    finally:
        try:
            if dispatcher is not None:
                await drain_dispatcher_update_tasks(dispatcher)
            if handlers is not None:
                await handlers.flush_albums()
        finally:
            if cleanup_loop is not None:
                cleanup_loop.stop()
            if scheduler is not None:
                scheduler.stop()
            if daily_stats_scheduler is not None:
                daily_stats_scheduler.stop()
            tasks = [task for task in (daily_stats_task, cleanup_task, scheduler_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if bot is not None:
                    await bot.session.close()
            finally:
                await database.close()


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    configure_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
