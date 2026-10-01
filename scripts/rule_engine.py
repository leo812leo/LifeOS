"""scripts/rule_engine.py — 訓練準備度決策引擎。

根據 Garmin 健康數據，用確定性規則決定今天的建議訓練強度。
此模組不呼叫任何外部 API，僅依賴本地數據和閾值。

決策優先順序（第一個匹配的規則勝出）：
1. Body Battery < 30 → 完全休息
2. Training Readiness < 40 → 休息/恢復
3. 睡眠優先：7 日睡眠債 > 6h 或昨晚 < 4.5h → 最高只能輕鬆跑（T18）
4. 睡眠 < 6 小時 → 最高只能輕鬆跑
5. GLP-1 注射後 24-48 小時 → 最高只能輕鬆跑
6. Training Readiness 40-60 + 低 HRV → 輕鬆跑
7. Training Readiness 40-60 + 正常 HRV → 中等強度
8. Training Readiness > 60 + 好 HRV → 高強度 OK
9. Training Readiness > 60 + 低 HRV → 中等強度

疊加警告（不影響主等級，但提醒注意）：
- ACWR > 1.3 → 過度訓練風險
- ACWR < 0.8 → 可增加訓練量
- 恢復時間 > 48h → 恢復未完成
- 壓力 > 60 → 高壓力
- HRV 狀態 LOW → 低 HRV 警告

使用方式::

    from scripts.rule_engine import evaluate
    from models import HealthData, AthleteProfile, RuleConfig

    verdict = evaluate(
        health=health_data,
        profile=AthleteProfile(),
        config=RuleConfig(),
        today=date.today(),
        chronic_load=350.0,
        sleep_debt_7d=8.5,
    )
    print(verdict.readiness_level)  # "HIGH" / "MODERATE" / "EASY" / "REST"
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import (
    AthleteProfile,
    HealthData,
    RuleConfig,
    RuleVerdict,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ── 常數 ─────────────────────────────────────────────────────────────────────


_LEVEL_REST = "REST"
_LEVEL_EASY = "EASY"
_LEVEL_MODERATE = "MODERATE"
_LEVEL_HIGH = "HIGH"

_ZONE_MAP = {
    _LEVEL_REST: 1,
    _LEVEL_EASY: 2,
    _LEVEL_MODERATE: 3,
    _LEVEL_HIGH: 5,
}

_INTENSITY_DESC = {
    _LEVEL_REST: "完全休息或輕度伸展",
    _LEVEL_EASY: "輕鬆跑或散步",
    _LEVEL_MODERATE: "輕鬆跑或輕量重訓",
    _LEVEL_HIGH: "可進行高強度訓練",
}


# ── 主函式 ───────────────────────────────────────────────────────────────────


def evaluate(
    health: HealthData,
    profile: AthleteProfile,
    config: RuleConfig,
    today: date,
    chronic_load: Optional[float] = None,
    sleep_debt_7d: Optional[float] = None,
) -> RuleVerdict:
    """評估今日訓練建議。

    Args:
        health: 今日（或昨日）Garmin 健康數據。
        profile: 運動員個人檔案。
        config: 規則引擎閾值設定。
        today: 今天日期（用於判斷 GLP-1 注射日）。
        chronic_load: 過去 28 天平均訓練負荷（來自 Notion 歷史）。
            為 None 時跳過 ACWR 檢查。
        sleep_debt_7d: 近 7 天累積睡眠債（小時，見
            ``utils.sleep.sleep_debt_7d``）。為 None 時跳過睡眠債檢查，
            只保留既有的「昨晚 < ``sleep_downgrade_hours``」規則（T18）。

    Returns:
        RuleVerdict 包含建議等級、警告和原始分數。
    """
    warnings: List[str] = []
    flags: List[str] = []

    # ── 計算原始分數 ──────────────────────────────────────────────────────

    hrv_ratio = _calc_hrv_ratio(health)
    acwr = _calc_acwr(health.acute_load, chronic_load)

    raw_scores = [
        ("training_readiness", float(health.training_readiness or 0)),
        ("hrv_ratio", hrv_ratio if hrv_ratio is not None else 0.0),
        ("acwr", acwr if acwr is not None else 0.0),
        ("sleep_hours", float(health.sleep_hours or 0)),
        ("body_battery", float(health.body_battery or 0)),
        ("stress", float(health.stress or 0)),
        ("sleep_debt_7d", float(sleep_debt_7d) if sleep_debt_7d is not None else 0.0),
    ]

    # ── 優先順序決策（第一個匹配即停止）────────────────────────────────────

    level = _LEVEL_MODERATE  # 預設值

    # Rule 1: Body Battery 極低 → 完全休息
    if health.body_battery is not None and health.body_battery < config.body_battery_rest:
        level = _LEVEL_REST
        flags.append("low_body_battery")
        logger.info(
            "Rule 1: Body Battery %d < %d → REST",
            health.body_battery,
            config.body_battery_rest,
        )

    # Rule 2: Training Readiness 極低 → 休息
    elif (
        health.training_readiness is not None
        and health.training_readiness < config.readiness_rest
    ):
        level = _LEVEL_REST
        flags.append("low_readiness")
        logger.info(
            "Rule 2: Training Readiness %d < %d → REST",
            health.training_readiness,
            config.readiness_rest,
        )

    # Rule 3: 睡眠優先——7 日睡眠債過高或昨晚嚴重不足 → 降級到 EASY（T18）
    # 涵蓋「昨晚睡眠尚可、但長期累積債務已高」的情境（既有 Rule 4 只看昨晚
    # 一晚，抓不到這種慢性債務）；昨晚 < sleep_acute_downgrade_hours（比
    # Rule 4 的門檻更嚴格）則不論債務資料是否存在都直接觸發。
    elif (
        sleep_debt_7d is not None and sleep_debt_7d > config.sleep_debt_downgrade_hours
    ) or (
        health.sleep_hours is not None
        and health.sleep_hours < config.sleep_acute_downgrade_hours
    ):
        level = _LEVEL_EASY
        flags.append("sleep_debt")
        logger.info(
            "Rule 3: Sleep debt %.1fh (>%.1fh) or last night %.1fh (<%.1fh) → EASY",
            sleep_debt_7d if sleep_debt_7d is not None else -1.0,
            config.sleep_debt_downgrade_hours,
            health.sleep_hours if health.sleep_hours is not None else -1.0,
            config.sleep_acute_downgrade_hours,
        )

    # Rule 4: 睡眠不足 → 降級到 EASY
    elif (
        health.sleep_hours is not None
        and health.sleep_hours < config.sleep_downgrade_hours
    ):
        level = _LEVEL_EASY
        flags.append("sleep_deficit")
        logger.info(
            "Rule 4: Sleep %.1fh < %.1fh → EASY",
            health.sleep_hours,
            config.sleep_downgrade_hours,
        )

    # Rule 5: GLP-1 注射窗口 → 降級到 EASY
    elif _in_glp1_window(today, profile.glp1_injection_day, config.glp1_cooldown_hours):
        level = _LEVEL_EASY
        flags.append("glp1_window")
        logger.info("Rule 5: GLP-1 injection window → EASY")

    # Rule 6-9: 依 Training Readiness + HRV 組合判斷
    elif health.training_readiness is not None:
        if health.training_readiness <= config.readiness_moderate:
            # Readiness 40-60
            if hrv_ratio is not None and hrv_ratio < (1.0 - config.hrv_deficit_pct):
                level = _LEVEL_EASY
                logger.info(
                    "Rule 6: Readiness %d (40-60) + low HRV ratio %.2f → EASY",
                    health.training_readiness,
                    hrv_ratio,
                )
            else:
                level = _LEVEL_MODERATE
                logger.info(
                    "Rule 7: Readiness %d (40-60) + normal HRV → MODERATE",
                    health.training_readiness,
                )
        else:
            # Readiness > 60
            if hrv_ratio is not None and hrv_ratio < (1.0 - config.hrv_deficit_pct):
                level = _LEVEL_MODERATE
                logger.info(
                    "Rule 9: Readiness %d (>60) + low HRV ratio %.2f → MODERATE",
                    health.training_readiness,
                    hrv_ratio,
                )
            else:
                level = _LEVEL_HIGH
                logger.info(
                    "Rule 8: Readiness %d (>60) + good HRV → HIGH",
                    health.training_readiness,
                )
    else:
        # Training Readiness 不可用 → 保守策略
        level = _LEVEL_MODERATE
        flags.append("no_readiness_data")
        logger.warning("Training Readiness unavailable → default MODERATE")

    # ── 疊加警告 ─────────────────────────────────────────────────────────

    if acwr is not None:
        if acwr > config.acwr_overtraining:
            warnings.append(
                f"過度訓練風險：ACWR {acwr:.2f} > {config.acwr_overtraining}"
            )
        elif acwr < config.acwr_undertraining:
            flags.append("can_increase_load")
    else:
        if chronic_load is None:
            flags.append("insufficient_history")

    if (
        health.recovery_time is not None
        and health.recovery_time > config.recovery_warning_hours
    ):
        warnings.append(f"恢復未完成：還需 {health.recovery_time} 小時")

    if health.stress is not None and health.stress > config.stress_warning:
        warnings.append(f"高壓力：平均壓力 {health.stress}/100")

    if health.hrv_status is not None and health.hrv_status.upper() == "LOW":
        warnings.append("HRV 狀態偏低")

    # ── 組裝結果 ─────────────────────────────────────────────────────────

    verdict = RuleVerdict(
        readiness_level=level,
        recommended_intensity=_INTENSITY_DESC[level],
        max_hr_zone=_ZONE_MAP[level],
        warnings=tuple(warnings),
        flags=tuple(flags),
        raw_scores=tuple(
            (k, v) for k, v in raw_scores
        ),
    )

    logger.info(
        "Verdict: %s (zone %d) | warnings=%d flags=%s",
        verdict.readiness_level,
        verdict.max_hr_zone,
        len(verdict.warnings),
        verdict.flags,
    )

    return verdict


# ── 內部工具函式 ─────────────────────────────────────────────────────────────


def _calc_hrv_ratio(health: HealthData) -> Optional[float]:
    """計算 HRV 昨晚值 / 七日均值的比率。

    Args:
        health: 健康數據。

    Returns:
        比率（1.0 = 與基線相同），資料不足時回傳 None。
    """
    if health.hrv_last_night is None or health.hrv_weekly_avg is None:
        return None
    if health.hrv_weekly_avg == 0:
        return None
    return health.hrv_last_night / health.hrv_weekly_avg


def _calc_acwr(
    acute_load: Optional[int],
    chronic_load: Optional[float],
) -> Optional[float]:
    """計算急性慢性訓練負荷比（ACWR）。

    Args:
        acute_load: 今日急性負荷。
        chronic_load: 28 天慢性負荷均值。

    Returns:
        ACWR 比值，資料不足時回傳 None。
    """
    if acute_load is None or chronic_load is None:
        return None
    if chronic_load == 0:
        return None
    return acute_load / chronic_load


def _in_glp1_window(
    today: date,
    injection_day: int,
    cooldown_hours: int,
) -> bool:
    """判斷今天是否在 GLP-1 注射後的冷卻窗口內。

    檢查今天和前一天是否為注射日（涵蓋 24-48 小時窗口）。

    Args:
        today: 今天日期。
        injection_day: 注射日（0=週一, 6=週日）。
        cooldown_hours: 冷卻時數（用於判斷涵蓋幾天）。

    Returns:
        若在冷卻窗口內回傳 True。
    """
    cooldown_days = max(1, cooldown_hours // 24)
    for offset in range(cooldown_days):
        check_date = today - timedelta(days=offset)
        if check_date.weekday() == injection_day:
            return True
    return False
