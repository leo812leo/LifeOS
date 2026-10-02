"""tests/test_weekly_review.py — weekly_review.py 的單元測試。

聚焦驗證：
- T15：建議遵循率計算的接線（純程式，不經 AI）。
- T18：睡眠趨勢計算的接線（純程式，不經 AI）——本週/上週睡眠均值比較、
  趨勢箭頭、與週回顧訊息的附加。

Notion / AI API 全部 mock 在 HTTP/API 邊界或模組函式邊界
（比照 tests/test_ai_coach.py 的模式），不對真實服務發送請求。
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ActivityData, HealthData
from utils.adherence import AdviceLogEntry
from scripts.weekly_review import (
    _compute_week_adherence,
    _compute_week_sleep_trend,
    _fetch_week_activities_by_date,
    run_weekly_review,
)


# ── _fetch_week_activities_by_date ───────────────────────────────────────────


class TestFetchWeekActivitiesByDate:
    def test_returns_empty_when_activity_db_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("ACTIVITY_DB_ID", raising=False)
        result = _fetch_week_activities_by_date(date(2026, 7, 6), date(2026, 7, 12))
        assert result == {}

    def test_groups_activities_by_date(self, monkeypatch) -> None:
        monkeypatch.setenv("ACTIVITY_DB_ID", "fake-db")
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")

        acts = [
            ActivityData(start_time="2026-07-06T06:00:00.000-00:00", training_load=10.0),
            ActivityData(start_time="2026-07-06T18:00:00.000-00:00", training_load=20.0),
            ActivityData(start_time="2026-07-08", training_load=50.0),
        ]
        with patch("scripts.weekly_review.get_notion_client", return_value=MagicMock()), patch(
            "scripts.notion_reader.fetch_activity_history", return_value=acts
        ):
            result = _fetch_week_activities_by_date(date(2026, 7, 6), date(2026, 7, 12))

        assert len(result["2026-07-06"]) == 2
        assert len(result["2026-07-08"]) == 1

    def test_excludes_activities_outside_week_range(self, monkeypatch) -> None:
        monkeypatch.setenv("ACTIVITY_DB_ID", "fake-db")
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")

        acts = [ActivityData(start_time="2026-06-01", training_load=10.0)]
        with patch("scripts.weekly_review.get_notion_client", return_value=MagicMock()), patch(
            "scripts.notion_reader.fetch_activity_history", return_value=acts
        ):
            result = _fetch_week_activities_by_date(date(2026, 7, 6), date(2026, 7, 12))

        assert result == {}

    def test_query_failure_returns_empty_dict(self, monkeypatch) -> None:
        monkeypatch.setenv("ACTIVITY_DB_ID", "fake-db")
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")

        with patch("scripts.weekly_review.get_notion_client", side_effect=Exception("boom")):
            result = _fetch_week_activities_by_date(date(2026, 7, 6), date(2026, 7, 12))

        assert result == {}


# ── _compute_week_adherence ──────────────────────────────────────────────────


class TestComputeWeekAdherence:
    def test_returns_none_when_no_advice_entries(self) -> None:
        with patch("scripts.weekly_review.read_advice_log", return_value=[]):
            assert _compute_week_adherence(date(2026, 7, 6), date(2026, 7, 12)) is None

    def test_computes_line_from_fake_advice_and_activity_data(self) -> None:
        entries = [
            AdviceLogEntry(date="2026-07-06", verdict="rest"),
            AdviceLogEntry(date="2026-07-07", verdict="reduce"),
        ]
        activities_by_date = {
            "2026-07-06": [ActivityData(training_load=5.0)],   # rest → 遵循
            "2026-07-07": [ActivityData(training_load=150.0)],  # 仍硬練 → 不遵循
        }
        with patch(
            "scripts.weekly_review.read_advice_log", return_value=entries
        ), patch(
            "scripts.weekly_review._fetch_week_activities_by_date",
            return_value=activities_by_date,
        ):
            line = _compute_week_adherence(date(2026, 7, 6), date(2026, 7, 12))

        assert line is not None
        assert "1/2" in line
        assert "休息建議 1/1" in line
        assert "降載 0/1" in line

    def test_returns_none_when_exception_raised(self) -> None:
        with patch(
            "scripts.weekly_review.read_advice_log", side_effect=Exception("boom")
        ):
            assert _compute_week_adherence(date(2026, 7, 6), date(2026, 7, 12)) is None


# ── run_weekly_review（整合走線）──────────────────────────────────────────────


class TestRunWeeklyReviewAdherenceLine:
    def test_dry_run_appends_adherence_line_to_review(self, monkeypatch) -> None:
        monkeypatch.setattr("scripts.weekly_review.append_run", MagicMock())

        with patch(
            "scripts.weekly_review._fetch_active_krs", return_value=[]
        ), patch(
            "scripts.weekly_review._build_week_summary", return_value={}
        ), patch(
            "scripts.weekly_review._generate_weekly_review", return_value="本週回顧內容"
        ), patch(
            "scripts.weekly_review._compute_week_adherence",
            return_value="📊 本週遵循 5/7（休息建議 2/2 遵循，降載 1/2）",
        ), patch(
            "scripts.weekly_review._compute_week_sleep_trend", return_value=None
        ):
            result = run_weekly_review(dry_run=True)

        assert result is not None
        assert "本週回顧內容" in result
        assert "📊 本週遵循 5/7" in result

    def test_dry_run_without_adherence_data_leaves_review_unchanged(self, monkeypatch) -> None:
        monkeypatch.setattr("scripts.weekly_review.append_run", MagicMock())

        with patch(
            "scripts.weekly_review._fetch_active_krs", return_value=[]
        ), patch(
            "scripts.weekly_review._build_week_summary", return_value={}
        ), patch(
            "scripts.weekly_review._generate_weekly_review", return_value="本週回顧內容"
        ), patch(
            "scripts.weekly_review._compute_week_adherence", return_value=None
        ), patch(
            "scripts.weekly_review._compute_week_sleep_trend", return_value=None
        ):
            result = run_weekly_review(dry_run=True)

        assert result == "本週回顧內容"

    def test_review_generation_failure_skips_adherence_and_returns_none(
        self, monkeypatch
    ) -> None:
        mock_append_run = MagicMock()
        monkeypatch.setattr("scripts.weekly_review.append_run", mock_append_run)

        with patch(
            "scripts.weekly_review._fetch_active_krs", return_value=[]
        ), patch(
            "scripts.weekly_review._build_week_summary", return_value={}
        ), patch(
            "scripts.weekly_review._generate_weekly_review", return_value=None
        ), patch(
            "scripts.weekly_review._compute_week_adherence"
        ) as mock_adherence, patch(
            "scripts.weekly_review._compute_week_sleep_trend"
        ) as mock_sleep_trend:
            result = run_weekly_review(dry_run=True)

        assert result is None
        mock_adherence.assert_not_called()
        mock_sleep_trend.assert_not_called()
        mock_append_run.assert_not_called()


# ── _compute_week_sleep_trend（T18）───────────────────────────────────────


class TestComputeWeekSleepTrend:
    def test_returns_none_when_no_health_records(self) -> None:
        with patch(
            "scripts.weekly_review._fetch_health_records_for_sleep_trend",
            return_value=[],
        ):
            assert _compute_week_sleep_trend(date(2026, 7, 6), date(2026, 7, 12)) is None

    def test_computes_line_with_avg_debt_and_trend_arrow(self) -> None:
        this_week = [HealthData(sleep_hours=5.0)] * 7  # avg 5.0h, debt 14h
        last_week = [HealthData(sleep_hours=7.0)] * 7  # avg 7.0h (better last week)
        with patch(
            "scripts.weekly_review._fetch_health_records_for_sleep_trend",
            return_value=this_week + last_week,
        ):
            line = _compute_week_sleep_trend(date(2026, 7, 6), date(2026, 7, 12))

        assert line is not None
        assert "睡眠" in line
        assert "5.0h" in line
        assert "14.0h" in line
        assert "趨勢 ↓" in line  # 這週睡得比上週差

    def test_trend_arrow_up_when_improved(self) -> None:
        this_week = [HealthData(sleep_hours=7.5)] * 7
        last_week = [HealthData(sleep_hours=5.0)] * 7
        with patch(
            "scripts.weekly_review._fetch_health_records_for_sleep_trend",
            return_value=this_week + last_week,
        ):
            line = _compute_week_sleep_trend(date(2026, 7, 6), date(2026, 7, 12))

        assert line is not None
        assert "趨勢 ↑" in line

    def test_no_trend_note_when_no_prior_week_data(self) -> None:
        this_week = [HealthData(sleep_hours=7.0)] * 7
        with patch(
            "scripts.weekly_review._fetch_health_records_for_sleep_trend",
            return_value=this_week,
        ):
            line = _compute_week_sleep_trend(date(2026, 7, 6), date(2026, 7, 12))

        assert line is not None
        assert "趨勢" not in line

    def test_returns_none_when_exception_raised(self) -> None:
        with patch(
            "scripts.weekly_review._fetch_health_records_for_sleep_trend",
            side_effect=Exception("boom"),
        ):
            assert _compute_week_sleep_trend(date(2026, 7, 6), date(2026, 7, 12)) is None


class TestRunWeeklyReviewSleepLine:
    def test_dry_run_appends_sleep_trend_line_to_review(self, monkeypatch) -> None:
        """驗收情境：weekly_review dry-run 輸出含睡眠趨勢行。"""
        monkeypatch.setattr("scripts.weekly_review.append_run", MagicMock())

        with patch(
            "scripts.weekly_review._fetch_active_krs", return_value=[]
        ), patch(
            "scripts.weekly_review._build_week_summary", return_value={}
        ), patch(
            "scripts.weekly_review._generate_weekly_review", return_value="本週回顧內容"
        ), patch(
            "scripts.weekly_review._compute_week_adherence", return_value=None
        ), patch(
            "scripts.weekly_review._compute_week_sleep_trend",
            return_value="😴 睡眠：7 日均 5.0h（債務 14.0h，趨勢 ↓）",
        ):
            result = run_weekly_review(dry_run=True)

        assert result is not None
        assert "本週回顧內容" in result
        assert "睡眠：7 日均 5.0h" in result
