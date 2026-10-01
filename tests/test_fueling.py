"""tests/test_fueling.py — utils/fueling 長跑補給協議純函式的單元測試（T11）。

全部是純函式（只讀 ``RACE_DATE`` 環境變數），無外部 API。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ActivityData
from utils.fueling import (
    LONG_RUN_MIN_DISTANCE_KM,
    LONG_RUN_MIN_DURATION_MIN,
    carb_target_g_per_hr,
    format_fuel_history,
    fuel_target_line,
    get_race_date,
    is_long_run,
    parse_workout_text,
    weeks_to_race,
)


# ── get_race_date / weeks_to_race ────────────────────────────────────────────


class TestRaceDate:
    def test_reads_race_date_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("RACE_DATE", "2026-12-01")
        assert get_race_date() == date(2026, 12, 1)

    def test_invalid_race_date_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("RACE_DATE", "not-a-date")
        assert get_race_date() == date(2030, 1, 1)

    def test_missing_race_date_uses_default(self, monkeypatch) -> None:
        monkeypatch.delenv("RACE_DATE", raising=False)
        assert get_race_date() == date(2030, 1, 1)

    def test_weeks_to_race_counts_full_weeks(self, monkeypatch) -> None:
        monkeypatch.setenv("RACE_DATE", "2026-10-25")
        assert weeks_to_race(today=date(2026, 7, 11)) == 15  # 106 天 // 7
        assert weeks_to_race(today=date(2026, 10, 25)) == 0

    def test_weeks_to_race_never_negative(self, monkeypatch) -> None:
        monkeypatch.setenv("RACE_DATE", "2026-10-25")
        assert weeks_to_race(today=date(2026, 11, 30)) == 0


# ── carb_target_g_per_hr（漸進協議）──────────────────────────────────────────


class TestCarbTarget:
    @pytest.mark.parametrize(
        "weeks_left,expected",
        [
            (16, (30, 40)),
            (13, (30, 40)),  # 邊界：≥13 起步量
            (12, (40, 60)),  # 邊界：9-12
            (9, (40, 60)),
            (8, (60, 75)),  # 邊界：5-8
            (5, (60, 75)),
            (4, (60, 90)),  # 邊界：≤4 完整賽事演練
            (0, (60, 90)),
        ],
    )
    def test_progression_tiers(self, weeks_left: int, expected: tuple) -> None:
        assert carb_target_g_per_hr(weeks_left) == expected

    def test_targets_never_exceed_race_range(self) -> None:
        """任何週數的目標都不得超過賽事目標上限 90g/hr、低於起步量 30g/hr。"""
        for weeks in range(0, 30):
            lo, hi = carb_target_g_per_hr(weeks)
            assert 30 <= lo <= hi <= 90

    def test_progression_is_monotonic(self) -> None:
        """越接近賽事目標量只增不減（腸胃訓練必須漸進）。"""
        prev_lo, prev_hi = carb_target_g_per_hr(30)
        for weeks in range(29, -1, -1):
            lo, hi = carb_target_g_per_hr(weeks)
            assert lo >= prev_lo
            assert hi >= prev_hi
            prev_lo, prev_hi = lo, hi


# ── is_long_run ──────────────────────────────────────────────────────────────


class TestIsLongRun:
    def test_duration_at_threshold_is_long_run(self) -> None:
        assert is_long_run(LONG_RUN_MIN_DURATION_MIN, None) is True

    def test_duration_below_threshold_is_not(self) -> None:
        assert is_long_run(60.0, None) is False

    def test_distance_fallback_when_no_duration(self) -> None:
        assert is_long_run(None, LONG_RUN_MIN_DISTANCE_KM) is True
        assert is_long_run(None, 8.0) is False

    def test_both_none_is_not_long_run(self) -> None:
        """資料缺失時寧可不觸發補給提醒。"""
        assert is_long_run(None, None) is False


# ── parse_workout_text（T17：coach_week.json 手動排程沒有結構化距離/時間）───


class TestParseWorkoutText:
    def test_extracts_distance_km(self) -> None:
        assert parse_workout_text("18km @ 7:00/km") == (None, 18.0)

    def test_extracts_duration_minutes(self) -> None:
        assert parse_workout_text("90min easy") == (90.0, None)

    def test_extracts_both(self) -> None:
        assert parse_workout_text("90min / 14km") == (90.0, 14.0)

    def test_no_match_returns_none_none(self) -> None:
        assert parse_workout_text("輕鬆跑") == (None, None)

    def test_none_input_returns_none_none(self) -> None:
        assert parse_workout_text(None) == (None, None)

    def test_chinese_minute_unit(self) -> None:
        assert parse_workout_text("90分鐘慢跑") == (90.0, None)


# ── fuel_target_line ─────────────────────────────────────────────────────────


class TestFuelTargetLine:
    def test_contains_quantified_target_and_fuel_command_hint(self, monkeypatch) -> None:
        monkeypatch.setenv("RACE_DATE", "2026-10-25")
        line = fuel_target_line(today=date(2026, 7, 11))  # 距賽 15 週 → 30-40g
        assert "30-40g" in line
        assert "距賽 15 週" in line
        assert "/fuel" in line
        assert "\n" not in line  # 一行訊息

    def test_target_scales_with_weeks_to_race(self, monkeypatch) -> None:
        monkeypatch.setenv("RACE_DATE", "2026-10-25")
        line_near = fuel_target_line(today=date(2026, 10, 5))  # 距賽 2 週
        assert "60-90g" in line_near


# ── format_fuel_history ──────────────────────────────────────────────────────


class TestFormatFuelHistory:
    def test_no_fuel_records_says_none_explicitly(self) -> None:
        records = [ActivityData(start_time="2026-07-10", distance_km=10.0)]
        section = format_fuel_history(records)
        assert "無" in section
        assert "/fuel" in section

    def test_formats_fuel_record_with_rate(self) -> None:
        records = [
            ActivityData(
                start_time="2026-07-08T06:30:00.000+08:00",
                distance_km=18.0,
                duration_min=90.0,
                fuel_carbs_g=45.0,
                gi_score=2,
                fuel_plan="兩包gel+500ml電解質",
            ),
        ]
        section = format_fuel_history(records)
        assert "2026-07-08" in section
        assert "碳水 45g" in section
        assert "30g/hr" in section  # 45g / 1.5hr = 30
        assert "GI 2" in section
        assert "兩包gel" in section

    def test_high_gi_gets_warning_marker(self) -> None:
        records = [ActivityData(start_time="2026-07-08", fuel_carbs_g=60.0, gi_score=4)]
        section = format_fuel_history(records)
        assert "GI 4⚠️" in section

    def test_filters_out_records_without_fuel_data(self) -> None:
        records = [
            ActivityData(start_time="2026-07-10", rpe=7),  # 無補給資料
            ActivityData(start_time="2026-07-08", fuel_carbs_g=45.0, gi_score=2),
        ]
        section = format_fuel_history(records)
        assert "2026-07-10" not in section
        assert "2026-07-08" in section

    def test_respects_limit(self) -> None:
        records = [
            ActivityData(start_time=f"2026-07-{10 - i:02d}", fuel_carbs_g=40.0)
            for i in range(7)
        ]
        section = format_fuel_history(records, limit=3)
        assert section.count("碳水") == 3
