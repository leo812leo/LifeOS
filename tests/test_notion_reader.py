"""tests/test_notion_reader.py — notion_reader 的 Notion property 解析單元測試。

聚焦驗證 T05 修復：
- Activity DB 的 ``Name`` 是 title 型別，需用 ``_get_title`` 解析（非 rich_text）。
- ``Date`` 屬性要被讀入 ``ActivityData.start_time``。

也順便覆蓋 health/nutrition 解析與 fetch_* 函式（mock 在 query_pages 邊界，
比照 tests/test_ai_coach.py 的模式）。
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ActivityData, HealthData, NutritionData
from scripts.notion_reader import (
    _get_date,
    _get_number,
    _get_rich_text,
    _get_select,
    _get_title,
    _parse_activity_page,
    _parse_health_page,
    _parse_nutrition_page,
    fetch_activity_history,
    rolling_resting_hr,
)


# ── Notion property 假資料建構器 ─────────────────────────────────────────────


def _title_prop(text: str) -> dict:
    """建構 title 型別的 Notion property payload。"""
    return {"title": [{"text": {"content": text}}]}


def _rich_text_prop(text: str) -> dict:
    """建構 rich_text 型別的 Notion property payload。"""
    return {"rich_text": [{"text": {"content": text}}]}


def _number_prop(value: float) -> dict:
    return {"number": value}


def _select_prop(name: str) -> dict:
    return {"select": {"name": name}}


def _date_prop(start: str) -> dict:
    return {"date": {"start": start}}


# ── _get_title ───────────────────────────────────────────────────────────────


class TestGetTitle:
    def test_parses_title_content(self) -> None:
        props = {"Name": _title_prop("晨跑 10K")}
        assert _get_title(props, "Name") == "晨跑 10K"

    def test_missing_property_returns_none(self) -> None:
        assert _get_title({}, "Name") is None

    def test_empty_title_array_returns_none(self) -> None:
        props = {"Name": {"title": []}}
        assert _get_title(props, "Name") is None

    def test_rich_text_payload_does_not_leak_into_title(self) -> None:
        """title helper 讀 rich_text 型別 payload 應回 None（型別不匹配）。"""
        props = {"Name": _rich_text_prop("not a title")}
        assert _get_title(props, "Name") is None


# ── _get_rich_text（既有行為的回歸測試）──────────────────────────────────────


class TestGetRichText:
    def test_parses_rich_text_content(self) -> None:
        props = {"Pace": _rich_text_prop("6:51")}
        assert _get_rich_text(props, "Pace") == "6:51"

    def test_title_payload_does_not_leak_into_rich_text(self) -> None:
        """rich_text helper 讀 title 型別 payload 應回 None — 這正是原始 bug。"""
        props = {"Name": _title_prop("晨跑 10K")}
        assert _get_rich_text(props, "Name") is None


# ── _get_date ────────────────────────────────────────────────────────────────


class TestGetDate:
    def test_parses_date_start(self) -> None:
        props = {"Date": _date_prop("2026-07-10T06:30:00.000+08:00")}
        assert _get_date(props, "Date") == "2026-07-10T06:30:00.000+08:00"

    def test_missing_date_returns_none(self) -> None:
        assert _get_date({}, "Date") is None


# ── _parse_activity_page（核心修復驗證）──────────────────────────────────────


class TestParseActivityPage:
    def test_activity_name_parsed_from_title(self) -> None:
        props = {
            "Name": _title_prop("晨跑 10K"),
            "Type": _select_prop("running"),
        }
        result = _parse_activity_page(props)
        assert isinstance(result, ActivityData)
        assert result.activity_name == "晨跑 10K"

    def test_start_time_parsed_from_date(self) -> None:
        props = {
            "Name": _title_prop("晨跑 10K"),
            "Date": _date_prop("2026-07-10T06:30:00.000+08:00"),
        }
        result = _parse_activity_page(props)
        assert result.start_time == "2026-07-10T06:30:00.000+08:00"

    def test_missing_name_and_date_default_to_none(self) -> None:
        result = _parse_activity_page({})
        assert result.activity_name is None
        assert result.start_time is None

    def test_full_activity_page_parses_all_fields(self) -> None:
        props = {
            "Activity ID": _number_prop(123456789),
            "Name": _title_prop("長跑 21K"),
            "Type": _select_prop("running"),
            "Date": _date_prop("2026-07-10T06:30:00.000+08:00"),
            "Distance (km)": _number_prop(21.1),
            "Duration (min)": _number_prop(120.5),
            "Pace": _rich_text_prop("5:42"),
            "Avg HR": _number_prop(145),
            "Max HR": _number_prop(168),
            "Calories": _number_prop(1450),
            "Avg Cadence": _number_prop(172.3),
            "Avg Power": _number_prop(280),
            "Elevation Gain": _number_prop(85),
            "TE Aerobic": _number_prop(3.8),
            "TE Anaerobic": _number_prop(1.2),
            "VO2 Max": _number_prop(45),
            "Training Load": _number_prop(210.5),
        }
        result = _parse_activity_page(props)

        assert result.activity_id == 123456789
        assert result.activity_name == "長跑 21K"
        assert result.activity_type == "running"
        assert result.start_time == "2026-07-10T06:30:00.000+08:00"
        assert result.distance_km == 21.1
        assert result.duration_min == 120.5
        assert result.pace == "5:42"
        assert result.avg_hr == 145
        assert result.max_hr == 168
        assert result.calories == 1450
        assert result.avg_cadence == 172.3
        assert result.avg_power == 280
        assert result.elevation_gain == 85
        assert result.training_effect_aerobic == 3.8
        assert result.training_effect_anaerobic == 1.2
        assert result.vo2max == 45
        assert result.training_load == 210.5

    def test_rpe_niggle_and_notes_parsed(self) -> None:
        """T07：RPE / Niggle Score / Notes 三個新欄位要能正確解析。"""
        props = {
            "Name": _title_prop("晨跑 10K"),
            "RPE": _number_prop(7),
            "Niggle Score": _number_prop(4),
            "Notes": _rich_text_prop("[RPE] 小腿有點緊\n[Niggle] 左膝"),
        }
        result = _parse_activity_page(props)
        assert result.rpe == 7
        assert result.niggle_score == 4
        assert result.notes == "[RPE] 小腿有點緊\n[Niggle] 左膝"

    def test_rpe_niggle_and_notes_default_to_none(self) -> None:
        result = _parse_activity_page({})
        assert result.rpe is None
        assert result.niggle_score is None
        assert result.notes is None

    def test_fuel_fields_parsed(self) -> None:
        """T11：Fuel Carbs (g) / Fuel Plan / GI Score 三個補給欄位要能正確解析。"""
        props = {
            "Name": _title_prop("長跑 18K"),
            "Fuel Carbs (g)": _number_prop(45),
            "Fuel Plan": _rich_text_prop("兩包gel+500ml電解質"),
            "GI Score": _number_prop(2),
        }
        result = _parse_activity_page(props)
        assert result.fuel_carbs_g == 45
        assert result.fuel_plan == "兩包gel+500ml電解質"
        assert result.gi_score == 2

    def test_fuel_fields_default_to_none(self) -> None:
        """遷移前（Activity DB 尚無補給欄位）解析不得炸掉，一律 None。"""
        result = _parse_activity_page({})
        assert result.fuel_carbs_g is None
        assert result.fuel_plan is None
        assert result.gi_score is None


# ── _parse_health_page / _parse_nutrition_page（順手掃一遍）──────────────────


class TestParseHealthPage:
    def test_parses_expected_fields(self) -> None:
        props = {
            "Steps": _number_prop(10234),
            "Resting HR": _number_prop(52),
            "Sleep Hours": _number_prop(7.5),
            "Sleep Score": _number_prop(85),
            "Stress Avg": _number_prop(30),
            "Body Battery": _number_prop(65),
            "Active Calories": _number_prop(620),
            "Readiness Level": _select_prop("HIGH"),
            "HRV Status": _select_prop("BALANCED"),
        }
        result = _parse_health_page(props)
        assert isinstance(result, HealthData)
        assert result.steps == 10234
        assert result.resting_heart_rate == 52
        assert result.sleep_hours == 7.5
        assert result.sleep_score == 85
        assert result.training_readiness_level == "HIGH"
        assert result.hrv_status == "BALANCED"

    def test_missing_fields_default_to_none(self) -> None:
        result = _parse_health_page({})
        assert result.steps is None
        assert result.training_readiness_level is None


class TestRollingRestingHr:
    """rolling_resting_hr 的測試 — Karvonen 心率區間用的近 N 天安靜心率均值（T13）。"""

    def test_averages_recent_window(self) -> None:
        records = [
            HealthData(resting_heart_rate=60),
            HealthData(resting_heart_rate=58),
            HealthData(resting_heart_rate=56),
        ]
        assert rolling_resting_hr(records, window=3) == 58.0

    def test_only_uses_first_window_records(self) -> None:
        """假設清單已由新到舊排序，只取前 window 筆（近 N 天）。"""
        records = [
            HealthData(resting_heart_rate=60),
            HealthData(resting_heart_rate=60),
            HealthData(resting_heart_rate=100),  # 超出 window，不應納入
        ]
        assert rolling_resting_hr(records, window=2) == 60.0

    def test_skips_none_values(self) -> None:
        records = [
            HealthData(resting_heart_rate=None),
            HealthData(resting_heart_rate=60),
            HealthData(resting_heart_rate=58),
        ]
        assert rolling_resting_hr(records, window=3) == 59.0

    def test_empty_records_returns_none(self) -> None:
        assert rolling_resting_hr([]) is None

    def test_all_none_returns_none(self) -> None:
        records = [HealthData(resting_heart_rate=None), HealthData(resting_heart_rate=None)]
        assert rolling_resting_hr(records) is None

    def test_default_window_is_7(self) -> None:
        records = [HealthData(resting_heart_rate=50 + i) for i in range(10)]
        # 前 7 筆：50..56，均值 53.0
        assert rolling_resting_hr(records) == 53.0


class TestParseNutritionPage:
    def test_parses_expected_fields(self) -> None:
        props = {
            "Date": _date_prop("2026-07-10"),
            "Calories": _number_prop(2200),
            "Protein (g)": _number_prop(160),
            "Weight (kg)": _number_prop(98.5),
        }
        result = _parse_nutrition_page(props)
        assert isinstance(result, NutritionData)
        assert result.date == "2026-07-10"
        assert result.calories == 2200
        assert result.protein_g == 160
        assert result.weight_kg == 98.5

    def test_missing_fields_default_to_none(self) -> None:
        result = _parse_nutrition_page({})
        assert result.date is None
        assert result.calories is None


# ── fetch_activity_history（mock query_pages 邊界）───────────────────────────


class TestFetchActivityHistory:
    def test_parses_pages_into_activity_data_with_name_and_start_time(self) -> None:
        fake_pages = [
            {
                "properties": {
                    "Name": _title_prop("晨跑 10K"),
                    "Type": _select_prop("running"),
                    "Date": _date_prop("2026-07-10T06:30:00.000+08:00"),
                    "Distance (km)": _number_prop(10.0),
                }
            },
            {
                "properties": {
                    "Name": _title_prop("重訓"),
                    "Type": _select_prop("strength_training"),
                    "Date": _date_prop("2026-07-09T18:00:00.000+08:00"),
                }
            },
        ]
        with patch(
            "notion_client_helper.query_pages", return_value=fake_pages
        ) as mock_query:
            results = fetch_activity_history(client=object(), database_id="db123", days=7)

        mock_query.assert_called_once()
        assert len(results) == 2
        assert all(r.activity_name is not None for r in results)
        assert all(r.start_time is not None for r in results)
        assert results[0].activity_name == "晨跑 10K"
        assert results[0].start_time == "2026-07-10T06:30:00.000+08:00"
        assert results[1].activity_name == "重訓"

    def test_empty_activity_db_returns_empty_list(self) -> None:
        """Activity DB 當天沒有活動列時要優雅回傳空清單，不可假設資料存在。"""
        with patch("notion_client_helper.query_pages", return_value=[]):
            results = fetch_activity_history(client=object(), database_id="db123", days=7)

        assert results == []
