"""tests/test_sleep.py — utils/sleep.py 的單元測試（T18：睡眠一級管理）。

聚焦驗證：
- 環境變數讀取（SLEEP_TARGET_H / WEEKDAY_WAKE / WEEKEND_RUN_START）與防呆退回預設值。
- ``sleep_debt_7d``：累積睡眠債計算、缺資料天數不計債的啟發式、視窗切片。
- ``build_sleep_section``：驗收情境（連續 7 天 5h → 觸發＋顯示；7 天 7.5h → 不觸發但仍顯示）、
  平日/週末就寢目標切換、晚練日訓練結束時間建議。
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import HealthData, RuleConfig
from utils.sleep import (
    build_sleep_section,
    get_sleep_target_h,
    get_weekday_wake,
    get_weekend_run_start,
    sleep_debt_7d,
)


# ── 環境變數讀取 ─────────────────────────────────────────────────────────────


class TestGetSleepTargetH:
    def test_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("SLEEP_TARGET_H", raising=False)
        assert get_sleep_target_h() == 7.0

    def test_reads_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("SLEEP_TARGET_H", "7.5")
        assert get_sleep_target_h() == 7.5

    def test_invalid_value_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("SLEEP_TARGET_H", "not-a-number")
        assert get_sleep_target_h() == 7.0


class TestGetWeekdayWake:
    def test_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("WEEKDAY_WAKE", raising=False)
        wake = get_weekday_wake()
        assert (wake.hour, wake.minute) == (6, 30)

    def test_reads_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("WEEKDAY_WAKE", "07:15")
        wake = get_weekday_wake()
        assert (wake.hour, wake.minute) == (7, 15)

    def test_malformed_value_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("WEEKDAY_WAKE", "garbage")
        wake = get_weekday_wake()
        assert (wake.hour, wake.minute) == (6, 30)


class TestGetWeekendRunStart:
    def test_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("WEEKEND_RUN_START", raising=False)
        wake = get_weekend_run_start()
        assert (wake.hour, wake.minute) == (6, 0)

    def test_reads_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("WEEKEND_RUN_START", "05:45")
        wake = get_weekend_run_start()
        assert (wake.hour, wake.minute) == (5, 45)


# ── sleep_debt_7d ────────────────────────────────────────────────────────────


class TestSleepDebt7d:
    def test_empty_records_returns_none(self) -> None:
        assert sleep_debt_7d([]) is None

    def test_all_missing_sleep_hours_returns_none(self) -> None:
        records = [HealthData(sleep_hours=None) for _ in range(7)]
        assert sleep_debt_7d(records) is None

    def test_seven_days_at_5h_gives_14h_debt(self) -> None:
        """驗收情境 1：連續 7 天 5h 睡眠 → 債務 14h（target 7.0h）。"""
        records = [HealthData(sleep_hours=5.0) for _ in range(7)]
        assert sleep_debt_7d(records) == pytest.approx(14.0)

    def test_seven_days_at_7_5h_gives_zero_debt(self) -> None:
        """驗收情境 2：連續 7 天 7.5h 睡眠 → 無債務（0.0，非 None）。"""
        records = [HealthData(sleep_hours=7.5) for _ in range(7)]
        assert sleep_debt_7d(records) == pytest.approx(0.0)

    def test_missing_days_do_not_count_toward_debt(self) -> None:
        """缺資料天數不計債（啟發式）：3 筆 5h + 4 筆 None → 只算 3 天。"""
        records = [HealthData(sleep_hours=5.0)] * 3 + [HealthData(sleep_hours=None)] * 4
        # 3 天各欠 2h（target 7.0h）= 6h；缺資料的 4 天不計入。
        assert sleep_debt_7d(records) == pytest.approx(6.0)

    def test_window_limits_to_first_n_records(self) -> None:
        """只取前 window 筆（假設新到舊排序），超出視窗的紀錄不影響債務。"""
        records = [HealthData(sleep_hours=5.0)] * 7 + [HealthData(sleep_hours=0.0)] * 20
        assert sleep_debt_7d(records, window=7) == pytest.approx(14.0)

    def test_custom_target_h_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("SLEEP_TARGET_H", "7.0")
        records = [HealthData(sleep_hours=6.0)] * 7
        # target_h=6.0 覆寫 env 的 7.0 → 每天剛好達標，債務為 0。
        assert sleep_debt_7d(records, target_h=6.0) == pytest.approx(0.0)

    def test_debt_never_negative_for_oversleeping(self) -> None:
        records = [HealthData(sleep_hours=9.0)] * 7
        assert sleep_debt_7d(records) == pytest.approx(0.0)


# ── build_sleep_section ──────────────────────────────────────────────────────


class TestBuildSleepSection:
    def test_seven_days_at_5h_triggers_priority_and_shows_bedtime(self) -> None:
        """驗收情境 1：連續 7 天 5h → 債務 14h、含「睡眠優先」與就寢目標。"""
        records = [HealthData(sleep_hours=5.0, sleep_score=60)] * 7
        section = build_sleep_section(records, today=date(2026, 7, 8))  # Wed

        assert "睡眠優先" in section
        assert "14.0h" in section
        assert "就寢目標" in section

    def test_seven_days_at_7_5h_does_not_trigger_but_shows_bedtime(self) -> None:
        """驗收情境 2：連續 7 天 7.5h → 不觸發，就寢行仍顯示（管理是常態）。"""
        records = [HealthData(sleep_hours=7.5, sleep_score=88)] * 7
        section = build_sleep_section(records, today=date(2026, 7, 8))  # Wed

        assert "睡眠優先" not in section
        assert "就寢目標" in section
        assert "昨晚睡眠" in section

    def test_empty_records_shows_no_data_and_still_shows_bedtime(self) -> None:
        section = build_sleep_section([], today=date(2026, 7, 8))
        assert "昨晚睡眠：無資料" in section
        assert "就寢目標" in section
        assert "睡眠優先" not in section

    def test_acute_single_night_triggers_even_without_debt_history(self) -> None:
        """OR 邏輯：只有 1 筆紀錄（債務小），但昨晚 < 4.5h 仍應觸發。"""
        records = [HealthData(sleep_hours=4.0, sleep_score=40)]
        section = build_sleep_section(records, today=date(2026, 7, 8))
        assert "睡眠優先" in section

    def test_last_night_score_included_when_present(self) -> None:
        records = [HealthData(sleep_hours=6.2, sleep_score=71)]
        section = build_sleep_section(records, today=date(2026, 7, 8))
        assert "6.2h" in section
        assert "71" in section

    def test_weekday_includes_training_cutoff_line(self) -> None:
        """今天是週三（晚練日）→ 附訓練結束時間建議。"""
        records = [HealthData(sleep_hours=7.0)]
        section = build_sleep_section(records, today=date(2026, 7, 8))  # Wed
        assert "今晚訓練請於" in section
        assert "前結束以保就寢目標" in section

    def test_weekend_has_no_training_cutoff_line(self) -> None:
        """今天是週六 → 不附晚練截止時間（今早長跑，非晚練）。"""
        records = [HealthData(sleep_hours=7.0)]
        section = build_sleep_section(records, today=date(2026, 7, 11))  # Sat
        assert "今晚訓練請於" not in section

    def test_tomorrow_weekend_uses_weekend_wake_time(self, monkeypatch) -> None:
        """今天週五 → 明天週六（週末）→ 就寢目標依 WEEKEND_RUN_START 推算。"""
        monkeypatch.setenv("WEEKEND_RUN_START", "05:00")
        monkeypatch.setenv("SLEEP_TARGET_H", "7.0")
        records = [HealthData(sleep_hours=7.0)]
        section = build_sleep_section(records, today=date(2026, 7, 10))  # Fri
        # 05:00 起床 - 7h = 22:00 就寢目標
        assert "22:00" in section
        assert "05:00" in section

    def test_tomorrow_weekday_uses_weekday_wake_time(self, monkeypatch) -> None:
        """今天週日 → 明天週一（平日）→ 就寢目標依 WEEKDAY_WAKE 推算。"""
        monkeypatch.setenv("WEEKDAY_WAKE", "06:30")
        monkeypatch.setenv("SLEEP_TARGET_H", "7.0")
        records = [HealthData(sleep_hours=7.0)]
        section = build_sleep_section(records, today=date(2026, 7, 12))  # Sun
        # 06:30 起床 - 7h = 23:30 就寢目標（跨午夜捲動）
        assert "23:30" in section
        assert "06:30" in section

    def test_training_cutoff_is_two_hours_before_bedtime(self, monkeypatch) -> None:
        monkeypatch.setenv("WEEKDAY_WAKE", "06:30")
        monkeypatch.setenv("SLEEP_TARGET_H", "7.0")
        records = [HealthData(sleep_hours=7.0)]
        # 週三 → 明天週四（平日）→ 就寢 23:30 → 截止 21:30
        section = build_sleep_section(records, today=date(2026, 7, 8))
        assert "21:30" in section

    def test_custom_config_thresholds_change_trigger(self) -> None:
        """自訂 RuleConfig 閾值時，觸發條件跟著變（單一來源）。"""
        records = [HealthData(sleep_hours=6.8)] * 7  # debt = 7*0.2 = 1.4h
        strict_config = RuleConfig(sleep_debt_downgrade_hours=1.0)
        section = build_sleep_section(records, today=date(2026, 7, 8), config=strict_config)
        assert "睡眠優先" in section

        default_section = build_sleep_section(records, today=date(2026, 7, 8))
        assert "睡眠優先" not in default_section
