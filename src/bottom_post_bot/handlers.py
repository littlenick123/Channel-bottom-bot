from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .domain import ButtonSpec, ContentItem, DraftRevision, group_content_items
from .drafts import DraftService, IncomingContent
from .pending_drafts import PendingDraftService
from .permissions import PermissionDenied, PermissionService, PermissionUnavailable
from .repositories import AuthorizationError, Repository, ResourceLimitError
from .stats import format_chat_report


logger = logging.getLogger(__name__)

HELP_TEXT = """频道置底机器人

1. 把机器人加入频道并授予发帖、删除消息权限。
2. 在“我的频道”绑定公开用户名、频道 ID，或转发频道帖子。
3. 转发帖子给机器人保存为个人草稿，可配置 URL 按钮。
4. 把草稿发布到编号槽位；机器人按大编号到小编号发送，1 号位于最底部。

命令：/start /status /cancel /help"""

PENDING_UNAVAILABLE_MESSAGE = "该待确认内容已处理或已过期。"


def _cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _markup(rows) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _user_error_message(exc: Exception) -> str:
    if isinstance(exc, PermissionUnavailable):
        return exc.public_message
    return str(exc)


def parse_button_batch(value: str, existing: Sequence[ButtonSpec] = ()) -> tuple[ButtonSpec, ...]:
    """Parse URL button lines and return the complete, validated layout."""
    buttons = list(existing)
    row_counts: dict[int, int] = {}
    for button in buttons:
        row_counts[button.row] = row_counts.get(button.row, 0) + 1
    next_columns = {
        row: max(button.column for button in buttons if button.row == row) + 1
        for row in {button.row for button in buttons}
    }
    saw_button = False
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        saw_button = True
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
            raise ValueError(f"第 {line_number} 行：请使用：按钮文字 | URL | 行号")
        try:
            row = int(parts[2])
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行：按钮行号必须是正整数") from exc
        if row < 1:
            raise ValueError(f"第 {line_number} 行：按钮行号必须从 1 开始")
        row_index = row - 1
        if len(buttons) >= 100:
            raise ValueError(f"第 {line_number} 行：一条消息最多包含 100 个按钮")
        if row_counts.get(row_index, 0) >= 8:
            raise ValueError(f"第 {line_number} 行：一行最多包含 8 个按钮")
        try:
            button = ButtonSpec(parts[0], parts[1], row_index, next_columns.get(row_index, 0))
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行：{exc}") from exc
        buttons.append(button)
        row_counts[row_index] = row_counts.get(row_index, 0) + 1
        next_columns[row_index] = button.column + 1
    if not saw_button:
        raise ValueError("请至少输入一个按钮")
    DraftRevision(0, 1, (ContentItem(text="validate"),), tuple(buttons))
    return tuple(buttons)


def parse_button_input(value: str) -> ButtonSpec:
    buttons = parse_button_batch(value)
    if len(buttons) != 1:
        raise ValueError("请使用：按钮文字 | URL | 行号")
    return buttons[0]


def message_to_incoming(message: Message) -> IncomingContent:
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
    serialized = [entity.model_dump(mode="json", exclude_none=True) for entity in entities]
    media_kind: str | None = None
    file_id: str | None = None
    photos = getattr(message, "photo", None)
    if photos:
        media_kind, file_id = "photo", photos[-1].file_id
    else:
        for kind in ("video", "animation", "document", "audio", "voice", "sticker"):
            media = getattr(message, kind, None)
            if media is not None:
                media_kind, file_id = kind, media.file_id
                break
    return IncomingContent(
        source_chat_id=int(message.chat.id),
        source_message_id=int(message.message_id),
        text=text,
        media_kind=media_kind,
        grouped_id=str(message.media_group_id) if getattr(message, "media_group_id", None) else None,
        formatting_entities_json=json.dumps(serialized, ensure_ascii=False, separators=(",", ":")),
        file_id=file_id,
    )


class BotHandlers:
    def __init__(
        self,
        bot,
        repository: Repository,
        draft_service: DraftService,
        channel_service,
        permissions: PermissionService,
        scheduler,
        gateway,
        settings: Settings,
        pending_drafts: PendingDraftService,
        analytics=None,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.drafts = draft_service
        self.channels = channel_service
        self.permissions = permissions
        self.scheduler = scheduler
        self.gateway = gateway
        self.settings = settings
        self.pending_drafts = pending_drafts
        self.analytics = analytics
        self._album_messages: dict[tuple[int, str], list[Message]] = {}
        self._album_tasks: dict[tuple[int, str], asyncio.Task] = {}

    async def on_private_message(self, message: Message) -> None:
        if str(getattr(message.chat.type, "value", message.chat.type)) != "private":
            return
        if message.media_group_id:
            self._queue_album(message)
            return
        await self._handle_private_single(message)

    async def _handle_private_single(self, message: Message) -> None:
        if message.from_user is None:
            return
        user_id = int(message.from_user.id)
        display_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name]))
        await self.repository.upsert_user(user_id, display_name or str(user_id))
        text = (message.text or message.caption or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command == "/start":
            await self.repository.clear_conversation(user_id)
            await self.show_main(message)
            return
        if command == "/help":
            await message.answer(HELP_TEXT)
            return
        if command == "/cancel":
            state = await self.repository.get_conversation(user_id)
            await self.repository.clear_conversation(user_id)
            if state and state[0] == "await_pending_name":
                pending_id = int(state[1]["pending_id"])
                await message.answer("已取消命名，请选择如何处理待确认内容。", reply_markup=_markup(self._pending_buttons(pending_id)))
                return
            await message.answer("已取消当前操作。", reply_markup=_markup(self._main_buttons()))
            return
        if command == "/status":
            await self.show_status(message, user_id)
            return
        if command == "/stats":
            await self.show_stats(message, user_id)
            return
        if command == "/health":
            await self.show_health(message, user_id)
            return

        state = await self.repository.get_conversation(user_id)
        try:
            if state:
                await self._handle_state(message, user_id, state[0], state[1])
            elif self._is_forwarded(message):
                await self._capture(message, user_id, [message])
            else:
                await message.answer("请使用下方菜单；也可以直接把帖子转发给我保存为草稿。", reply_markup=_markup(self._main_buttons()))
        except (ValueError, PermissionDenied, AuthorizationError, ResourceLimitError) as exc:
            if isinstance(exc, AuthorizationError) and state and state[0] == "await_pending_name":
                await message.answer(f"操作失败：{PENDING_UNAVAILABLE_MESSAGE}")
                return
            await message.answer(f"操作失败：{_user_error_message(exc)}")
        except Exception:
            logger.exception("Private message handling failed", extra={"user_id": user_id})
            await message.answer("操作失败，详细原因已写入运行日志。请稍后重试或使用 /cancel。")

    def _queue_album(self, message: Message) -> None:
        key = (int(message.chat.id), str(message.media_group_id))
        self._album_messages.setdefault(key, []).append(message)
        previous = self._album_tasks.pop(key, None)
        if previous:
            previous.cancel()
        self._album_tasks[key] = asyncio.create_task(self._flush_album_after(key), name=f"album-{key[0]}-{key[1]}")

    async def _flush_album_after(self, key: tuple[int, str]) -> None:
        try:
            await asyncio.sleep(0.8)
            messages = sorted(self._album_messages.pop(key, []), key=lambda item: item.message_id)
            self._album_tasks.pop(key, None)
            if not messages or messages[0].from_user is None:
                return
            first = messages[0]
            user_id = int(first.from_user.id)
            await self.repository.upsert_user(user_id, first.from_user.full_name)
            await self._capture(first, user_id, messages)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Album capture failed", extra={"album_key": key})

    async def flush_albums(self) -> None:
        tasks = list(self._album_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def on_callback(self, event: CallbackQuery) -> None:
        if event.from_user is None or event.data is None:
            return
        user_id = int(event.from_user.id)
        await self.repository.upsert_user(user_id, event.from_user.full_name)
        try:
            await self._dispatch_callback(event, user_id, event.data)
            await event.answer()
        except (ValueError, PermissionDenied, AuthorizationError, ResourceLimitError) as exc:
            if isinstance(exc, AuthorizationError) and event.data.startswith("p:"):
                await event.answer(PENDING_UNAVAILABLE_MESSAGE, show_alert=True)
            else:
                await event.answer(_user_error_message(exc), show_alert=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
            await event.answer()
        except Exception:
            logger.exception("Callback handling failed", extra={"user_id": user_id, "callback": event.data})
            await event.answer("操作失败，请稍后重试", show_alert=True)

    async def show_main(self, event: Message | CallbackQuery) -> None:
        await self._show(event, "置底机器人管理中心\n\n草稿属于个人；频道配置由已绑定的当前频道管理员共享。", self._main_buttons())

    @staticmethod
    def _main_buttons():
        return [[_cb("📝 我的草稿", "d"), _cb("📣 我的频道", "c")], [_cb("ℹ️ 使用帮助", "h")]]

    @staticmethod
    def _pending_buttons(pending_id: int):
        return [[_cb("保存为草稿", f"p:s:{pending_id}"), _cb("保存并命名", f"p:n:{pending_id}")], [_cb("放弃", f"p:x:{pending_id}")]]

    async def _dispatch_callback(self, event: CallbackQuery, user_id: int, data: str) -> None:
        if data == "m":
            await self.show_main(event)
        elif data == "h":
            await self._show(event, HELP_TEXT, [[_cb("返回", "m")]])
        elif data == "d":
            await self._show_drafts(event, user_id)
        elif data == "d:new":
            await self._set_state(user_id, "await_draft", {})
            await self._show(event, "请转发或发送要保存的文本、媒体或相册。使用 /cancel 取消。")
        elif data.startswith("p:s:"):
            draft = await self.pending_drafts.confirm(int(data.rsplit(":", 1)[1]), user_id)
            await self._show_draft(event, user_id, draft.id)
        elif data.startswith("p:n:"):
            pending_id = int(data.rsplit(":", 1)[1])
            await self.pending_drafts.assert_confirmable(pending_id, user_id)
            await self._set_state(user_id, "await_pending_name", {"pending_id": pending_id})
            await self._show(event, "请输入草稿名称（1～100 个字符）。使用 /cancel 返回确认选项。")
        elif data.startswith("p:x:"):
            pending_id = int(data.rsplit(":", 1)[1])
            if not await self.pending_drafts.discard(pending_id, user_id):
                raise AuthorizationError("pending draft already processed or expired")
            await self._show_drafts(event, user_id)
        elif data.startswith("d:view:"):
            await self._show_draft(event, user_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("d:preview:"):
            await self._preview_draft(event, user_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("d:copy:"):
            draft_id = int(data.rsplit(":", 1)[1])
            await self._show_draft(event, user_id, (await self.drafts.copy(draft_id, user_id)).id)
        elif data.startswith("d:rename:"):
            draft_id = int(data.rsplit(":", 1)[1])
            await self._set_state(user_id, "await_rename", {"draft_id": draft_id})
            await self._show(event, "请发送新的草稿名称。")
        elif data.startswith("d:button:"):
            draft_id = int(data.rsplit(":", 1)[1])
            await self._set_state(user_id, "await_button", {"draft_id": draft_id})
            await self._show(
                event,
                "每行一个按钮：按钮文字 | URL | 行号\n"
                "官网 | https://example.com | 1\n"
                "客服 | tg://resolve?domain=example | 1\n"
                "下载 | https://example.com/d | 2",
            )
        elif data.startswith("d:buttons_clear:"):
            draft_id = int(data.rsplit(":", 1)[1])
            await self.drafts.update_buttons(draft_id, user_id, ())
            await self._show_draft(event, user_id, draft_id)
        elif data.startswith("d:delete_confirm:"):
            draft_id = int(data.rsplit(":", 1)[1])
            await self.repository.delete_draft(user_id, draft_id)
            await self.drafts.garbage_collect()
            await self._show_drafts(event, user_id)
        elif data.startswith("d:delete:"):
            draft_id = int(data.rsplit(":", 1)[1])
            await self._show(event, "确认删除个人草稿？已发布的频道快照不会受影响。", [[_cb("确认删除", f"d:delete_confirm:{draft_id}"), _cb("取消", f"d:view:{draft_id}")]])
        elif data == "c":
            await self._show_channels(event, user_id)
        elif data == "s":
            await self.show_stats(event, user_id)
        elif data.startswith("s:v:"):
            await self._show_stats_chat(event, user_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("s:t:"):
            _, _, channel_id, enabled = data.split(":")
            channel_id = int(channel_id)
            await self.permissions.assert_user_can_manage(user_id, channel_id)
            if not await self.repository.set_manager_stats_push_enabled(user_id, channel_id, enabled == "1"):
                raise AuthorizationError("频道绑定已失效")
            await self._show_stats_chat(event, user_id, channel_id)
        elif data == "c:bind":
            await self._set_state(user_id, "await_channel", {})
            await self._show(event, "请发送频道 @username、-100 开头的 ID，或转发一条频道帖子。")
        elif data.startswith("c:view:"):
            await self._show_channel(event, user_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("c:stats:"):
            await self._show_stats_chat(event, user_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith("c:slot_name:"):
            _, _, channel_id, slot = data.split(":")
            await self.permissions.assert_user_can_manage(user_id, int(channel_id))
            await self._set_state(user_id, "await_slot_name", {"channel_id": int(channel_id), "slot": int(slot)})
            await self._show(event, f"请发送 {slot} 号槽位的新名称（1～100 个字符）。")
        elif data.startswith("c:slot:"):
            _, _, channel_id, slot = data.split(":")
            await self._choose_draft(event, user_id, int(channel_id), int(slot))
        elif data.startswith("a:"):
            _, channel_id, slot, draft_id = data.split(":")
            draft = await self.repository.get_draft(user_id, int(draft_id))
            if not draft:
                raise PermissionDenied("草稿不存在或不属于你")
            await self.channels.assign_slot(int(channel_id), int(slot), draft.current_revision.id, user_id)
            await self.drafts.garbage_collect()
            await self.scheduler.request(int(channel_id), "slot-assigned", 0)
            await self._show_channel(event, user_id, int(channel_id))
        elif data.startswith("c:slot_toggle:"):
            _, _, channel_id, slot, enabled = data.split(":")
            await self.channels.set_slot_enabled(int(channel_id), int(slot), enabled == "1", user_id)
            await self.scheduler.request(int(channel_id), "slot-toggle", 0)
            await self._show_channel(event, user_id, int(channel_id))
        elif data.startswith("c:slot_clear:"):
            _, _, channel_id, slot = data.split(":")
            await self.channels.clear_slot(int(channel_id), int(slot), user_id)
            await self.drafts.garbage_collect()
            await self.scheduler.request(int(channel_id), "slot-clear", 0)
            await self._show_channel(event, user_id, int(channel_id))
        elif data.startswith("c:slot_move:"):
            _, _, channel_id, source = data.split(":")
            await self.permissions.assert_user_can_manage(user_id, int(channel_id))
            await self._set_state(user_id, "await_move_slot", {"channel_id": int(channel_id), "source": int(source)})
            await self._show(event, f"请输入 {source} 号槽位要移动到的目标编号。已有内容时将交换。")
        elif data.startswith("c:silent:"):
            _, _, channel_id, value = data.split(":")
            await self.channels.update_options(int(channel_id), user_id, silent=value == "1")
            await self.scheduler.request(int(channel_id), "silent-change", 0)
            await self._show_channel(event, user_id, int(channel_id))
        elif data.startswith("c:enabled:"):
            _, _, channel_id, value = data.split(":")
            await self.channels.update_options(int(channel_id), user_id, enabled=value == "1")
            await self.scheduler.request(int(channel_id), "enabled-change", 0)
            await self._show_channel(event, user_id, int(channel_id))
        elif data.startswith("c:refresh:"):
            channel_id = int(data.rsplit(":", 1)[1])
            await self.permissions.assert_user_can_manage(user_id, channel_id)
            await self.scheduler.request(channel_id, "manual", 0)
            await event.answer("已安排立即刷新", show_alert=True)
        elif data.startswith("c:delay:"):
            channel_id = int(data.rsplit(":", 1)[1])
            await self.permissions.assert_user_can_manage(user_id, channel_id)
            await self._set_state(user_id, "await_delay", {"channel_id": channel_id})
            await self._show(event, "请输入 1～3600 秒的刷新合并延迟。")
        elif data.startswith("c:resume:"):
            channel_id = int(data.rsplit(":", 1)[1])
            await self.permissions.assert_user_can_manage(user_id, channel_id)
            await self.repository.resume_channel(channel_id, user_id)
            await self.scheduler.request(channel_id, "resume", 0)
            await self._show_channel(event, user_id, channel_id)
        elif data.startswith("c:leave_confirm:"):
            channel_id = int(data.rsplit(":", 1)[1])
            await self.channels.leave(channel_id, user_id)
            await self._show_channels(event, user_id)
        elif data.startswith("c:leave:"):
            channel_id = int(data.rsplit(":", 1)[1])
            await self._show(event, "退出后你将看不到该频道配置，其他管理员不受影响。", [[_cb("确认退出", f"c:leave_confirm:{channel_id}"), _cb("取消", f"c:view:{channel_id}")]])
        elif data.startswith("c:delete_confirm:"):
            channel_id = int(data.rsplit(":", 1)[1])
            await self.permissions.assert_user_can_manage(user_id, channel_id)
            await self.repository.delete_channel_config(channel_id, user_id)
            await self._show_channels(event, user_id)
        elif data.startswith("c:delete:"):
            channel_id = int(data.rsplit(":", 1)[1])
            await self._show(event, "此操作会删除所有共享槽位和绑定，且影响所有管理员。确认继续？", [[_cb("永久删除", f"c:delete_confirm:{channel_id}"), _cb("取消", f"c:view:{channel_id}")]])

    async def _handle_state(self, message: Message, user_id: int, state: str, payload: dict) -> None:
        text = (message.text or message.caption or "").strip()
        if state == "await_draft":
            await self._capture(message, user_id, [message])
        elif state == "await_pending_name":
            if not 1 <= len(text) <= 100:
                raise ValueError("草稿名称长度必须为 1 到 100 个字符")
            try:
                draft = await self.pending_drafts.confirm(int(payload["pending_id"]), user_id, text)
            except AuthorizationError:
                await self.repository.clear_conversation(user_id)
                await message.answer(
                    PENDING_UNAVAILABLE_MESSAGE,
                    reply_markup=_markup(self._pending_buttons(int(payload["pending_id"]))),
                )
                return
            except ResourceLimitError as exc:
                await self.repository.clear_conversation(user_id)
                await message.answer(
                    f"操作失败：{exc}",
                    reply_markup=_markup(self._pending_buttons(int(payload["pending_id"]))),
                )
                return
            except Exception:
                await self.repository.clear_conversation(user_id)
                raise
            await self.repository.clear_conversation(user_id)
            await self._show_draft(message, user_id, draft.id)
        elif state == "await_rename":
            if not text:
                raise ValueError("草稿名称不能为空")
            if not await self.repository.rename_draft(user_id, int(payload["draft_id"]), text[:100]):
                raise PermissionDenied("草稿不存在")
            await self.repository.clear_conversation(user_id)
            await message.answer("已重命名。", reply_markup=_markup([[_cb("查看草稿", f"d:view:{payload['draft_id']}")]]))
        elif state == "await_button":
            draft = await self.repository.get_draft(user_id, int(payload["draft_id"]))
            if not draft:
                raise PermissionDenied("草稿不存在")
            buttons = parse_button_batch(text, draft.current_revision.buttons)
            added_count = len(buttons) - len(draft.current_revision.buttons)
            await self.drafts.update_buttons(draft.id, user_id, buttons)
            await self.repository.clear_conversation(user_id)
            await message.answer(f"已添加 {added_count} 个按钮。", reply_markup=_markup([[_cb("查看草稿", f"d:view:{draft.id}")]]))
        elif state == "await_channel":
            channel = await self.channels.bind(user_id, self._channel_reference(message))
            await self.repository.clear_conversation(user_id)
            await message.answer(f"已绑定频道：{channel.title}", reply_markup=_markup([[_cb("打开频道配置", f"c:view:{channel.id}")]]))
        elif state == "await_slot_name":
            if not 1 <= len(text) <= 100:
                raise ValueError("槽位名称长度必须为 1 到 100 个字符")
            channel_id = int(payload["channel_id"])
            await self.channels.rename_slot(channel_id, int(payload["slot"]), text, user_id)
            await self.repository.clear_conversation(user_id)
            await self._show_channel(message, user_id, channel_id)
        elif state == "await_delay":
            delay = int(text)
            channel_id = int(payload["channel_id"])
            await self.channels.update_options(channel_id, user_id, refresh_delay_seconds=delay)
            await self.repository.clear_conversation(user_id)
            await message.answer("刷新延迟已更新。", reply_markup=_markup([[_cb("返回频道", f"c:view:{channel_id}")]]))
        elif state == "await_move_slot":
            channel_id = int(payload["channel_id"])
            await self.channels.move_slot(channel_id, int(payload["source"]), int(text), user_id)
            await self.repository.clear_conversation(user_id)
            await self.scheduler.request(channel_id, "slot-move", 0)
            await message.answer("槽位编号已调整。", reply_markup=_markup([[_cb("返回频道", f"c:view:{channel_id}")]]))

    async def _capture(self, message: Message, user_id: int, messages: Sequence[Message]) -> None:
        pending = await self.pending_drafts.prepare(user_id, [message_to_incoming(item) for item in messages])
        await self.repository.clear_conversation(user_id)
        storage_ids = [item.storage_message_id for item in pending.items if item.storage_message_id is not None]
        try:
            await self.gateway.preview_storage_messages(user_id, storage_ids)
        except Exception:
            logger.exception("Pending-draft preview delivery failed", extra={"pending_draft_id": pending.id, "user_id": user_id})
            await message.answer("预览发送失败，但待确认内容已保存。")
        await message.answer("已保存预览，请确认是否保存为草稿。", reply_markup=_markup(self._pending_buttons(pending.id)))

    @staticmethod
    def _is_forwarded(message: Message) -> bool:
        return bool(getattr(message, "forward_origin", None) or getattr(message, "forward_from_chat", None))

    @staticmethod
    def _channel_reference(message: Message) -> str | int:
        text = (message.text or message.caption or "").strip()
        if text.startswith("@"):
            return text
        if text.lstrip("-").isdigit():
            return int(text)
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None) or getattr(message, "forward_from_chat", None)
        if chat is not None:
            return int(chat.id)
        raise ValueError("无法识别频道，请发送 @username 或 -100 开头的频道 ID")

    async def _show_drafts(self, event, user_id: int) -> None:
        drafts = await self.repository.list_drafts(user_id)
        rows = [[_cb(f"📝 {draft.name[:30]}", f"d:view:{draft.id}")] for draft in drafts]
        rows.extend([[_cb("➕ 保存新草稿", "d:new")], [_cb("返回", "m")]])
        await self._show(event, f"我的草稿（{len(drafts)}/{self.settings.max_drafts_per_user}）", rows)

    async def _show_draft(self, event, user_id: int, draft_id: int) -> None:
        draft = await self.repository.get_draft(user_id, draft_id)
        if not draft:
            raise PermissionDenied("草稿不存在或不属于你")
        text = f"草稿：{draft.name}\n版本：{draft.current_revision.revision_number}\n内容项：{len(draft.current_revision.items)}\n按钮：{len(draft.current_revision.buttons)}"
        rows = [
            [_cb("预览", f"d:preview:{draft.id}"), _cb("添加按钮", f"d:button:{draft.id}")],
            [_cb("清空按钮", f"d:buttons_clear:{draft.id}"), _cb("重命名", f"d:rename:{draft.id}")],
            [_cb("复制", f"d:copy:{draft.id}"), _cb("删除", f"d:delete:{draft.id}")],
            [_cb("返回草稿列表", "d")],
        ]
        await self._show(event, text, rows)

    async def _preview_draft(self, event: CallbackQuery, user_id: int, draft_id: int) -> None:
        draft = await self.repository.get_draft(user_id, draft_id)
        if not draft:
            raise PermissionDenied("草稿不存在")
        groups = group_content_items(draft.current_revision.items)
        for index, items in enumerate(groups):
            buttons = draft.current_revision.buttons if index == len(groups) - 1 else ()
            if len(items) > 1 and items[0].grouped_id:
                await self.gateway.send_content_group(user_id, items, buttons, silent=True)
            else:
                await self.gateway.send_content(user_id, items[0], buttons, silent=True)
        await event.answer("预览已发送", show_alert=True)

    async def _show_channels(self, event, user_id: int) -> None:
        channels = await self.repository.list_user_channels(user_id)
        rows = [[_cb(f"📣 {row['title'][:30]}", f"c:view:{row['id']}")] for row in channels]
        rows.extend([[_cb("➕ 绑定频道", "c:bind")], [_cb("返回", "m")]])
        await self._show(event, f"我的频道（{len(channels)}/{self.settings.max_channels_per_user}）", rows)

    def _stats_timezone(self) -> ZoneInfo:
        return ZoneInfo(getattr(self.settings, "stats_timezone", "Asia/Shanghai"))

    async def show_stats(self, event, user_id: int) -> None:
        if self.analytics is None:
            raise PermissionDenied("成员统计服务暂未启动")
        channels = await self.repository.list_user_channels(user_id)
        if not channels:
            await self._show(event, "尚未绑定频道或超级群组。", [[_cb("返回", "m")]])
        elif len(channels) == 1:
            await self._show_stats_chat(event, user_id, int(channels[0]["id"]))
        else:
            rows = [[_cb(f"📊 {str(row['title'])[:30]}", f"s:v:{int(row['id'])}")] for row in channels]
            rows.append([_cb("返回", "m")])
            await self._show(event, "请选择要查看成员统计的频道或超级群组：", rows)

    async def _show_stats_chat(self, event, user_id: int, channel_id: int) -> None:
        if self.analytics is None:
            raise PermissionDenied("成员统计服务暂未启动")
        await self.permissions.assert_user_can_manage(user_id, channel_id)
        report = await self.analytics.get_chat_report(user_id, channel_id, datetime.now(self._stats_timezone()))
        toggle_to = 0 if report.stats_push_enabled else 1
        label = "关闭每日推送" if report.stats_push_enabled else "开启每日推送"
        await self._show(
            event,
            format_chat_report(report, timezone=self._stats_timezone()),
            [[_cb(label, f"s:t:{channel_id}:{toggle_to}")], [_cb("返回统计列表", "s"), _cb("返回频道", f"c:view:{channel_id}")]],
        )

    async def _show_channel(self, event, user_id: int, channel_id: int) -> None:
        await self.permissions.assert_user_can_manage(user_id, channel_id)
        channel = await self.repository.get_channel(channel_id)
        if not channel:
            raise PermissionDenied("频道配置不存在")
        slots = await self.repository.list_channel_slots(channel_id)
        slot_map = {slot.slot_number: slot for slot in slots}
        lines = [f"频道：{channel['title']}", f"状态：{channel['status']}", f"总开关：{'开启' if channel['enabled'] else '关闭'}", f"发送：{'静默' if channel['silent'] else '通知'}", f"合并延迟：{channel['refresh_delay_seconds']} 秒", "槽位（发送顺序为大号→1号）："]
        for number in range(1, self.settings.max_slots_per_channel + 1):
            slot = slot_map.get(number)
            if slot:
                lines.append(
                    f"{number}. {slot.display_name or '未命名'}｜{'已启用' if slot.enabled else '已停用'}｜版本 {slot.revision.revision_number}"
                )
            else:
                lines.append(f"{number}. 空")
        rows = []
        for start in range(1, self.settings.max_slots_per_channel + 1, 5):
            rows.append(
                [
                    _cb(
                        f"{number} {(slot_map[number].display_name or '未命名')[:20]}" if number in slot_map else str(number),
                        f"c:slot:{channel_id}:{number}",
                    )
                    for number in range(start, min(start + 5, self.settings.max_slots_per_channel + 1))
                ]
            )
        for slot in slots:
            rows.append([_cb(f"{'停用' if slot.enabled else '启用'} {slot.slot_number}号", f"c:slot_toggle:{channel_id}:{slot.slot_number}:{0 if slot.enabled else 1}"), _cb(f"改名 {slot.slot_number}号", f"c:slot_name:{channel_id}:{slot.slot_number}"), _cb(f"清空 {slot.slot_number}号", f"c:slot_clear:{channel_id}:{slot.slot_number}"), _cb(f"移动 {slot.slot_number}号", f"c:slot_move:{channel_id}:{slot.slot_number}")])
        rows.extend([[_cb("立即刷新", f"c:refresh:{channel_id}"), _cb("修改延迟", f"c:delay:{channel_id}")], [_cb("改为通知" if channel["silent"] else "改为静默", f"c:silent:{channel_id}:{0 if channel['silent'] else 1}"), _cb("关闭" if channel["enabled"] else "开启", f"c:enabled:{channel_id}:{0 if channel['enabled'] else 1}")]])
        rows.append([_cb("成员统计", f"c:stats:{channel_id}")])
        if channel["status"] == "paused":
            rows.append([_cb("检查权限并恢复", f"c:resume:{channel_id}")])
        rows.extend([[_cb("退出管理", f"c:leave:{channel_id}"), _cb("删除共享配置", f"c:delete:{channel_id}")], [_cb("返回频道列表", "c")]])
        await self._show(event, "\n".join(lines), rows)

    async def _choose_draft(self, event, user_id: int, channel_id: int, slot: int) -> None:
        await self.permissions.assert_user_can_manage(user_id, channel_id)
        drafts = await self.repository.list_drafts(user_id)
        rows = [[_cb(draft.name[:30], f"a:{channel_id}:{slot}:{draft.id}")] for draft in drafts]
        rows.append([_cb("返回频道", f"c:view:{channel_id}")])
        await self._show(event, f"选择发布到 {slot} 号槽位的个人草稿：", rows)

    async def show_status(self, message: Message, user_id: int) -> None:
        drafts = await self.repository.list_drafts(user_id)
        channels = await self.repository.list_user_channels(user_id)
        await message.answer(f"个人状态\n草稿：{len(drafts)}\n已绑定频道：{len(channels)}")

    async def show_health(self, message: Message, user_id: int) -> None:
        if user_id not in self.settings.operator_user_ids:
            await message.answer("此命令仅供部署运维账号使用。")
            return
        counts = await self.repository.health_counts()
        await message.answer("运行状态：正常\n" + "\n".join(f"{key}: {value}" for key, value in counts.items()))

    async def _set_state(self, user_id: int, state: str, payload: dict) -> None:
        await self.repository.set_conversation(user_id, state, payload, time.time() + self.settings.conversation_timeout_seconds)

    @staticmethod
    async def _show(event: Message | CallbackQuery, text: str, rows=None) -> None:
        markup = _markup(rows)
        if isinstance(event, CallbackQuery):
            if event.message is None:
                return
            await event.message.edit_text(text, reply_markup=markup)
        else:
            await event.answer(text, reply_markup=markup)
