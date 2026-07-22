from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .domain import DailyMemberStats, MemberStatsReport


TELEGRAM_TEXT_LIMIT = 4096


def _signed(value: int) -> str:
    return f"{value:+d}"


def _day_line(label: str, stats: DailyMemberStats) -> str:
    return f"{label}：加入 {_signed(stats.joined_count)}｜离开 -{stats.left_count}｜净变化 {_signed(stats.net_change)}"


def _completeness(report: MemberStatsReport) -> str:
    incomplete = [stats for stats in (report.today, report.yesterday) if not stats.is_complete]
    if not incomplete:
        return "数据完整性：完整"
    reason = next((stats.incomplete_reason for stats in incomplete if stats.incomplete_reason), "统计数据不完整")
    return f"数据完整性：不完整（{reason[:400]}）"


def format_chat_report(report: MemberStatsReport, *, timezone: ZoneInfo | str = "Asia/Shanghai") -> str:
    """Render one bounded chat report suitable for a Telegram message or a daily digest."""
    tz = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    title = report.chat_title.replace("\n", " ").strip()[:200] or str(report.chat_id)
    count = "暂不可用" if report.current_member_count is None else str(report.current_member_count)
    updated = "暂不可用"
    if report.current_count_at is not None:
        updated = datetime.fromtimestamp(report.current_count_at, tz).strftime("%Y-%m-%d %H:%M")
    push = "已订阅" if report.stats_push_enabled else "已关闭"
    return "\n".join(
        (
            f"📊 {title}",
            f"当前成员：{count}",
            _day_line("今日", report.today),
            _day_line("昨日", report.yesterday),
            f"更新时间：{updated}",
            _completeness(report),
            f"推送：{push}",
        )
    )


def split_chat_reports(reports: list[str] | tuple[str, ...], *, header: str = "每日成员统计") -> list[str]:
    """Pack complete per-chat reports into Telegram-safe messages without splitting one report."""
    prefix = header.strip()[:500]
    chunks: list[str] = []
    current = prefix
    for report in reports:
        bounded = report[:TELEGRAM_TEXT_LIMIT]
        separator = "\n\n" if current else ""
        candidate = current + separator + bounded
        if len(candidate) <= TELEGRAM_TEXT_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = bounded
    if current:
        chunks.append(current)
    return chunks
