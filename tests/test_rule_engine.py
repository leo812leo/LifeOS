"""tests/test_rule_engine.py — 規則引擎單元測試。

覆蓋所有決策分支：
- Body Battery 極低 → REST
- Training Readiness 極低 → REST
- 睡眠不足 → EASY
- GLP-1 注射窗口 → EASY
- Readiness 40-60 + 低/正常 HRV → EASY/MODERATE
- Readiness > 60 + 好/低 HRV → HIGH/MODERATE
- ACWR 過高/過低 → 警告/旗標
- 資料不足 → 預設 MODERATE + 旗標
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import AthleteProfile, HealthData, RuleConfig
from scripts.rule_engine import evaluate


# ── 共用 Fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
def profile() -> AthleteProfile:
    """預設運動員檔案，GLP-1 注射日為週五（4）。"""
    return AthleteProfile(glp1_injection_day=4)


@pytest.fixture
def config() -> RuleConfig:
    """預設規則閾值。"""
    return RuleConfig()


# ── Rule 1: Body Battery < 30 → REST ────────────────────────────────────────


def test_body_battery_below_30_returns_rest(profile: AthleteProfile, config: RuleConfig) -> None:
    health = HealthData(body_battery=20, training_readiness=80, sleep_hours=8.0)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "REST"
    assert "low_body_battery" in verdict.flags


def test_body_battery_exactly_30_returns_moderate_not_rest(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(body_battery=30, training_readiness=50, sleep_hours=7.0)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level != "REST"


# ── Rule 2: Training Readiness < 40 → REST ──────────────────────────────────


def test_readiness_below_40_returns_rest(profile: AthleteProfile, config: RuleConfig) -> None:
    health = HealthData(body_battery=50, training_readiness=30, sleep_hours=7.0)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "REST"
    assert "low_readiness" in verdict.flags


# ── Rule 3: 睡眠優先——7 日睡眠債或昨晚嚴重不足 → EASY（T18）────────────────


def test_sleep_debt_over_threshold_triggers_sleep_debt_flag(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """昨晚睡眠尚可（6.5h，高於 Rule 4 的 6h 門檻），但 7 日債務過高仍應觸發。"""
    health = HealthData(body_battery=60, training_readiness=80, sleep_hours=6.5)
    verdict = evaluate(
        health, profile, config, today=date(2026, 4, 7), sleep_debt_7d=6.1
    )
    assert verdict.readiness_level == "EASY"
    assert "sleep_debt" in verdict.flags
    assert "sleep_deficit" not in verdict.flags


def test_sleep_debt_exactly_at_threshold_does_not_trigger(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(body_battery=60, training_readiness=80, sleep_hours=6.5)
    verdict = evaluate(
        health, profile, config, today=date(2026, 4, 7), sleep_debt_7d=6.0
    )
    assert verdict.readiness_level != "EASY"
    assert "sleep_debt" not in verdict.flags


def test_acute_night_below_4_5h_triggers_without_debt_data(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """sleep_debt_7d 未提供（None）時，昨晚嚴重不足仍應觸發（OR 邏輯的另一支）。"""
    health = HealthData(body_battery=60, training_readiness=80, sleep_hours=4.0)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "EASY"
    assert "sleep_debt" in verdict.flags


def test_acute_night_exactly_4_5h_falls_back_to_rule_4(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """4.5h 剛好不算「嚴重不足」，但仍 < 6h → 落回既有 Rule 4（sleep_deficit）。"""
    health = HealthData(body_battery=60, training_readiness=80, sleep_hours=4.5)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "EASY"
    assert "sleep_deficit" in verdict.flags
    assert "sleep_debt" not in verdict.flags


def test_raw_scores_includes_sleep_debt_7d(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(body_battery=60, training_readiness=80, sleep_hours=7.0)
    verdict = evaluate(
        health, profile, config, today=date(2026, 4, 7), sleep_debt_7d=3.5
    )
    assert ("sleep_debt_7d", 3.5) in verdict.raw_scores


def test_body_battery_still_overrides_sleep_debt(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """Body Battery 極低仍優先於睡眠債規則（既有優先序不變）。"""
    health = HealthData(body_battery=15, training_readiness=90, sleep_hours=8.0)
    verdict = evaluate(
        health, profile, config, today=date(2026, 4, 7), sleep_debt_7d=20.0
    )
    assert verdict.readiness_level == "REST"


# ── Rule 4 (was Rule 3): 睡眠 < 6h → EASY ─────────────────────────────────


def test_sleep_below_6h_downgrades_to_easy(profile: AthleteProfile, config: RuleConfig) -> None:
    health = HealthData(body_battery=60, training_readiness=80, sleep_hours=5.0)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "EASY"
    assert "sleep_deficit" in verdict.flags


# ── Rule 5 (was Rule 4): GLP-1 注射窗口 → EASY ─────────────────────────────


def test_glp1_injection_day_returns_easy(profile: AthleteProfile, config: RuleConfig) -> None:
    """注射日當天（週五 = weekday 4）。"""
    health = HealthData(body_battery=80, training_readiness=90, sleep_hours=8.0)
    friday = date(2026, 4, 3)  # 2026-04-03 is a Friday
    assert friday.weekday() == 4
    verdict = evaluate(health, profile, config, today=friday)
    assert verdict.readiness_level == "EASY"
    assert "glp1_window" in verdict.flags


def test_glp1_day_after_injection_returns_easy(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """注射隔天（週六）仍在 48h 窗口。"""
    health = HealthData(body_battery=80, training_readiness=90, sleep_hours=8.0)
    saturday = date(2026, 4, 4)  # Day after Friday injection
    assert saturday.weekday() == 5
    verdict = evaluate(health, profile, config, today=saturday)
    assert verdict.readiness_level == "EASY"
    assert "glp1_window" in verdict.flags


def test_glp1_two_days_after_returns_not_easy(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """注射後第三天（週日），超過 48h 窗口。"""
    health = HealthData(body_battery=80, training_readiness=90, sleep_hours=8.0)
    sunday = date(2026, 4, 5)
    assert sunday.weekday() == 6
    verdict = evaluate(health, profile, config, today=sunday)
    assert verdict.readiness_level == "HIGH"
    assert "glp1_window" not in verdict.flags


# ── Rule 5: Readiness 40-60 + 低 HRV → EASY ────────────────────────────────


def test_readiness_40_60_low_hrv_returns_easy(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=60,
        training_readiness=50,
        sleep_hours=7.0,
        hrv_last_night=35,
        hrv_weekly_avg=50,  # ratio = 0.70, < 0.85 threshold
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "EASY"


# ── Rule 6: Readiness 40-60 + 正常 HRV → MODERATE ──────────────────────────


def test_readiness_40_60_normal_hrv_returns_moderate(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=60,
        training_readiness=50,
        sleep_hours=7.0,
        hrv_last_night=48,
        hrv_weekly_avg=50,  # ratio = 0.96, >= 0.85 threshold
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "MODERATE"


# ── Rule 7: Readiness > 60 + 好 HRV → HIGH ─────────────────────────────────


def test_readiness_above_60_good_hrv_returns_high(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=80,
        training_readiness=75,
        sleep_hours=8.0,
        hrv_last_night=55,
        hrv_weekly_avg=50,  # ratio = 1.10, >= 0.85
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "HIGH"


# ── Rule 8: Readiness > 60 + 低 HRV → MODERATE ─────────────────────────────


def test_readiness_above_60_low_hrv_returns_moderate(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=80,
        training_readiness=75,
        sleep_hours=8.0,
        hrv_last_night=35,
        hrv_weekly_avg=50,  # ratio = 0.70, < 0.85
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "MODERATE"


# ── ACWR 警告 ────────────────────────────────────────────────────────────────


def test_acwr_above_1_3_adds_overtraining_warning(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=80,
        training_readiness=70,
        sleep_hours=7.5,
        acute_load=500,
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7), chronic_load=350.0)
    # ACWR = 500 / 350 ≈ 1.43
    assert any("過度訓練" in w for w in verdict.warnings)


def test_acwr_below_0_8_adds_can_increase_flag(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=80,
        training_readiness=70,
        sleep_hours=7.5,
        acute_load=200,
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7), chronic_load=350.0)
    # ACWR = 200 / 350 ≈ 0.57
    assert "can_increase_load" in verdict.flags


# ── 資料不足 ─────────────────────────────────────────────────────────────────


def test_no_chronic_load_flags_insufficient_history(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(body_battery=80, training_readiness=70, sleep_hours=7.5)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7), chronic_load=None)
    assert "insufficient_history" in verdict.flags


def test_no_readiness_data_defaults_moderate(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(body_battery=80, sleep_hours=7.5)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "MODERATE"
    assert "no_readiness_data" in verdict.flags


# ── 疊加警告 ─────────────────────────────────────────────────────────────────


def test_high_stress_adds_warning(profile: AthleteProfile, config: RuleConfig) -> None:
    health = HealthData(body_battery=80, training_readiness=70, sleep_hours=7.5, stress=75)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert any("壓力" in w for w in verdict.warnings)


def test_incomplete_recovery_adds_warning(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=80, training_readiness=70, sleep_hours=7.5, recovery_time=72
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert any("恢復" in w for w in verdict.warnings)


def test_low_hrv_status_adds_warning(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    health = HealthData(
        body_battery=80, training_readiness=70, sleep_hours=7.5, hrv_status="LOW"
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert any("HRV" in w for w in verdict.warnings)


# ── 多重警告組合 ─────────────────────────────────────────────────────────────


def test_multiple_warnings_combine(profile: AthleteProfile, config: RuleConfig) -> None:
    health = HealthData(
        body_battery=80,
        training_readiness=70,
        sleep_hours=7.5,
        stress=75,
        recovery_time=72,
        hrv_status="LOW",
        acute_load=500,
    )
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7), chronic_load=350.0)
    assert len(verdict.warnings) >= 3


# ── Rule 優先順序確認 ────────────────────────────────────────────────────────


def test_body_battery_overrides_high_readiness(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """即使 Readiness 很高，Body Battery 極低仍優先 REST。"""
    health = HealthData(body_battery=15, training_readiness=95, sleep_hours=9.0)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "REST"


def test_sleep_overrides_moderate_readiness(
    profile: AthleteProfile, config: RuleConfig
) -> None:
    """睡眠不足比 readiness 中等優先。"""
    health = HealthData(body_battery=60, training_readiness=55, sleep_hours=4.5)
    verdict = evaluate(health, profile, config, today=date(2026, 4, 7))
    assert verdict.readiness_level == "EASY"
    assert "sleep_deficit" in verdict.flags
