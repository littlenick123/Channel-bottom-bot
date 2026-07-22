from dataclasses import replace
from datetime import UTC, date, datetime

from bottom_post_bot.domain import DailyMemberStats, MemberStatsReport
from bottom_post_bot.stats import format_chat_report, split_chat_reports


def report(title: str = "News") -> MemberStatsReport:
    return MemberStatsReport(
        chat_id=-1001,
        chat_title=title,
        chat_type="channel",
        current_member_count=123,
        current_count_at=datetime(2026, 1, 2, 0, 5, tzinfo=UTC).timestamp(),
        today=DailyMemberStats(date(2026, 1, 2), joined_count=3, left_count=5),
        yesterday=DailyMemberStats(date(2026, 1, 1), joined_count=7, left_count=2, is_complete=False, incomplete_reason="runtime gap"),
        stats_push_enabled=True,
    )


def test_report_format_includes_counts_signed_nets_update_and_completeness() -> None:
    text = format_chat_report(report(), timezone="Asia/Shanghai")

    assert "频道/群组：News" in text
    assert "当前成员总数：123" in text
    assert "今天：新增 3｜退出 5｜净增 -2" in text
    assert "昨天：新增 7｜退出 2｜净增 +5" in text
    assert "更新时间：2026-01-02 08:05" in text
    assert "数据状态：不完整（runtime gap）" in text
    assert "每日推送：已开启" in text


def test_report_format_uses_required_labels_and_thousands_separators() -> None:
    member_report = replace(report(), current_member_count=12_345, stats_push_enabled=False)

    text = format_chat_report(member_report, timezone="Asia/Shanghai")

    assert "当前成员总数：12,345" in text
    assert "每日推送：已关闭" in text


def test_split_chat_reports_keeps_each_report_intact_and_below_telegram_limit() -> None:
    reports = [f"chat {number}\n" + "x" * 3000 for number in range(3)]

    chunks = split_chat_reports(reports, header="每日成员统计")

    assert len(chunks) == 3
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert all(f"chat {number}" in chunks[number] for number in range(3))
