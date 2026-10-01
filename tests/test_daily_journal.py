"""tests/test_daily_journal.py — daily_journal 的單元測試。"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.daily_journal import (
    create_journal_page,
    fetch_yesterday_health_summary,
)


# ── fetch_yesterday_health_summary ─────────────────────────────────────────────


class TestFetchYesterdayHealthSummary:
    def test_returns_summary_when_full_data(self) -> None:
        mock_page = {
            "properties": {
                "Steps":           {"number": 8000},
                "Sleep Hours":     {"number": 7.5},
                "Sleep Score":     {"number": 82},
                "Resting HR":      {"number": 58},
                "Stress Avg":      {"number": 30},
                "Body Battery":    {"number": 65},
                "Active Calories": {"number": 450},
            }
        }
        with (
            patch("scripts.daily_journal.HEALTH_DB_ID", "db-health"),
            patch("scripts.daily_journal.query_pages", return_value=[mock_page]),
        ):
            result = fetch_yesterday_health_summary("2024-01-14")

        assert result is not None
        assert "步數 8,000" in result
        assert "睡眠 7.5h" in result
        assert "睡眠分 82" in result
        assert "心率 58bpm" in result
        assert "壓力 30" in result
        assert "電量 65" in result
        assert "活動 450kcal" in result
        assert result.startswith("昨日健康：")

    def test_returns_partial_summary_on_missing_fields(self) -> None:
        mock_page = {
            "properties": {
                "Steps":       {"number": 5000},
                "Sleep Hours": {"number": 6.0},
            }
        }
        with (
            patch("scripts.daily_journal.HEALTH_DB_ID", "db-health"),
            patch("scripts.daily_journal.query_pages", return_value=[mock_page]),
        ):
            result = fetch_yesterday_health_summary("2024-01-14")

        assert result is not None
        assert "步數 5,000" in result
        assert "睡眠 6.0h" in result
        # 未提供的欄位不應出現
        assert "心率" not in result

    def test_returns_none_when_no_pages(self) -> None:
        with (
            patch("scripts.daily_journal.HEALTH_DB_ID", "db-health"),
            patch("scripts.daily_journal.query_pages", return_value=[]),
        ):
            result = fetch_yesterday_health_summary("2024-01-14")

        assert result is None

    def test_returns_none_when_health_db_not_configured(self) -> None:
        with patch("scripts.daily_journal.HEALTH_DB_ID", ""):
            result = fetch_yesterday_health_summary("2024-01-14")
        assert result is None

    def test_returns_none_when_all_numbers_missing(self) -> None:
        mock_page = {"properties": {}}
        with (
            patch("scripts.daily_journal.HEALTH_DB_ID", "db-health"),
            patch("scripts.daily_journal.query_pages", return_value=[mock_page]),
        ):
            result = fetch_yesterday_health_summary("2024-01-14")

        assert result is None


# ── create_journal_page ────────────────────────────────────────────────────────


class TestCreateJournalPage:
    def test_returns_true_when_page_already_exists(self) -> None:
        with (
            patch("scripts.daily_journal.JOURNAL_DB_ID", "db-journal"),
            patch(
                "scripts.daily_journal.page_exists_for_date", return_value=True
            ),
        ):
            result = create_journal_page("2024-01-15")

        assert result is True

    def test_creates_page_with_health_summary(self) -> None:
        with (
            patch("scripts.daily_journal.JOURNAL_DB_ID", "db-journal"),
            patch("scripts.daily_journal.page_exists_for_date", return_value=False),
            patch(
                "scripts.daily_journal.create_page",
                return_value={"id": "new-page"},
            ) as mock_create,
        ):
            result = create_journal_page("2024-01-15", health_summary="昨日健康：步數 8,000")

        assert result is True
        # 第一次 create_page 應帶 Health Summary
        call_properties = mock_create.call_args[0][2]
        assert "Health Summary" in call_properties
        assert "Name" in call_properties
        assert "Date" in call_properties

    def test_creates_page_without_health_summary(self) -> None:
        with (
            patch("scripts.daily_journal.JOURNAL_DB_ID", "db-journal"),
            patch("scripts.daily_journal.page_exists_for_date", return_value=False),
            patch(
                "scripts.daily_journal.create_page",
                return_value={"id": "new-page"},
            ) as mock_create,
        ):
            result = create_journal_page("2024-01-15")

        assert result is True
        call_properties = mock_create.call_args[0][2]
        assert "Health Summary" not in call_properties

    def test_falls_back_to_base_when_summary_write_fails(self) -> None:
        """Health Summary 欄位不存在時應退回只建立 Name + Date。"""
        call_count = 0

        def mock_create(client, db_id, properties):
            nonlocal call_count
            call_count += 1
            if "Health Summary" in properties:
                return None  # 第一次（帶 summary）失敗
            return {"id": "fallback-page"}  # 第二次（基本欄位）成功

        with (
            patch("scripts.daily_journal.JOURNAL_DB_ID", "db-journal"),
            patch("scripts.daily_journal.page_exists_for_date", return_value=False),
            patch("scripts.daily_journal.create_page", side_effect=mock_create),
        ):
            result = create_journal_page("2024-01-15", health_summary="昨日健康：步數 8,000")

        assert result is True
        assert call_count == 2  # 嘗試兩次

    def test_returns_false_when_all_writes_fail(self) -> None:
        with (
            patch("scripts.daily_journal.JOURNAL_DB_ID", "db-journal"),
            patch("scripts.daily_journal.page_exists_for_date", return_value=False),
            patch("scripts.daily_journal.create_page", return_value=None),
        ):
            result = create_journal_page("2024-01-15")

        assert result is False
