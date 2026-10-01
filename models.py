"""models.py — 全域共用的不可變資料模型。

所有腳本（health_tracker_web、rule_engine、ai_coach、daily_adjust 等）
都從這裡 import DTO，避免重複定義和循環依賴。

使用方式::

    from models import HealthData, ActivityData, RuleVerdict
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ── Garmin 健康數據 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthData:
    """每日健康與訓練摘要。

    Attributes:
        steps: 總步數。
        resting_heart_rate: 安靜心率（bpm）。
        sleep_hours: 總睡眠時數（小時）。
        sleep_score: 睡眠分數（0–100）。
        stress: 平均壓力值（0–100）。
        body_battery: 身體電量（當日最低值，0–100）。
        calories: 活動消耗卡路里（kcal）。
        training_readiness: 訓練準備度分數（0–100）。
        training_readiness_level: 訓練準備度等級（HIGH/MODERATE/LOW）。
        recovery_time: 恢復時間（小時）。
        hrv_last_night: 昨晚 HRV 平均值（ms）。
        hrv_weekly_avg: HRV 七日均值（ms）。
        hrv_status: HRV 狀態（BALANCED/LOW/HIGH）。
        acute_load: 急性訓練負荷（EPOC）。
        fitness_age: 體適能年齡。
    """

    steps: Optional[int] = None
    resting_heart_rate: Optional[int] = None
    sleep_hours: Optional[float] = None
    sleep_score: Optional[int] = None
    stress: Optional[int] = None
    body_battery: Optional[int] = None
    calories: Optional[int] = None
    training_readiness: Optional[int] = None
    training_readiness_level: Optional[str] = None
    recovery_time: Optional[int] = None
    hrv_last_night: Optional[int] = None
    hrv_weekly_avg: Optional[int] = None
    hrv_status: Optional[str] = None
    acute_load: Optional[int] = None
    fitness_age: Optional[float] = None


@dataclass(frozen=True)
class ActivityData:
    """單筆訓練活動。

    Attributes:
        activity_id: Garmin 活動 ID（用於防重複）。
        activity_name: 活動名稱。
        activity_type: 活動類型（running, cycling 等）。
        start_time: 開始時間（本地時間 ISO 字串）。
        distance_km: 距離（公里）。
        duration_min: 持續時間（分鐘）。
        pace: 配速字串（如 ``'6:51'``），非跑步類為 None。
        avg_hr: 平均心率（bpm）。
        max_hr: 最大心率（bpm）。
        calories: 消耗卡路里（kcal）。
        avg_cadence: 平均步頻（spm）。
        avg_power: 平均功率（W）。
        elevation_gain: 爬升（m）。
        training_effect_aerobic: 有氧訓練效果（0–5）。
        training_effect_anaerobic: 無氧訓練效果（0–5）。
        vo2max: VO2 Max。
        training_load: 訓練負荷（EPOC）。
        avg_stride_length: 平均步幅（cm）。
        avg_vertical_oscillation: 平均垂直振幅（cm）。
        avg_ground_contact_time: 平均觸地時間（ms）。
        rpe: 主觀自覺用力係數（1–10，跑者透過 /rpe 回報）。
        niggle_score: 主觀痠痛/傷害前兆分數（0=無異樣, 5=疼痛影響跑姿，
            透過 /niggle 回報）。
        notes: 備註（RPE/Niggle 的文字說明會附加於此欄位）。
        fuel_carbs_g: 該次訓練賽中總攝取碳水（g，透過 /fuel 回報；腸胃訓練）。
        fuel_plan: 補給計劃/實際內容（如「每 30min 一包 gel + 500ml 電解質」）。
        gi_score: 腸胃耐受分數（1=完全沒事, 5=嚴重不適，透過 /fuel 回報）。
    """

    activity_id: Optional[int] = None
    activity_name: Optional[str] = None
    activity_type: Optional[str] = None
    start_time: Optional[str] = None
    distance_km: Optional[float] = None
    duration_min: Optional[float] = None
    pace: Optional[str] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    calories: Optional[int] = None
    avg_cadence: Optional[float] = None
    avg_power: Optional[int] = None
    elevation_gain: Optional[int] = None
    training_effect_aerobic: Optional[float] = None
    training_effect_anaerobic: Optional[float] = None
    vo2max: Optional[int] = None
    training_load: Optional[float] = None
    avg_stride_length: Optional[float] = None
    avg_vertical_oscillation: Optional[float] = None
    avg_ground_contact_time: Optional[float] = None
    rpe: Optional[int] = None
    niggle_score: Optional[int] = None
    notes: Optional[str] = None
    fuel_carbs_g: Optional[float] = None
    fuel_plan: Optional[str] = None
    gi_score: Optional[int] = None


# ── 營養 / 體重數據（來自 Apple Health → iOS 捷徑 → Notion Nutrition DB）────────


@dataclass(frozen=True)
class NutritionData:
    """每日營養與體重摘要。

    來源：Apple Health（Cal AI / Water Tracker / 輕牛健康 / 體脂計）→ iOS 捷徑 → Notion。

    Attributes:
        date: 日期（ISO 格式 YYYY-MM-DD）。
        calories: 攝取總熱量（kcal）。
        protein_g: 蛋白質（g）。
        carbs_g: 碳水化合物（g）。
        fat_g: 脂肪（g）。
        water_ml: 飲水量（ml）。
        weight_kg: 體重（kg）。
        body_fat_pct: 體脂率（%）。
    """

    date: Optional[str] = None
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    water_ml: Optional[float] = None
    weight_kg: Optional[float] = None
    body_fat_pct: Optional[float] = None


# ── 運動員檔案 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AthleteProfile:
    """運動員個人檔案，用於規則引擎和 AI 教練的個人化。

    Attributes:
        weight_kg: 目前體重（kg）。
        target_weight_kg: 目標體重（kg）。
        vdot: Jack Daniels VDOT 值。
        max_hr: 最大心率（bpm）。
        glp1_injection_day: GLP-1 注射日（0=週一, 6=週日）。
        glp1_severity: GLP-1 副作用嚴重度
            （"minimal"=幾乎無感, "mild"=輕微, "moderate"=中等, "severe"=嚴重）。
            AI 會根據此調整訓練強度建議的保守程度。
        easy_pace_min: 輕鬆跑配速下限（min/km），如 6.5 = 6:30。
        easy_pace_max: 輕鬆跑配速上限（min/km），如 7.0 = 7:00。
        run_days: 每週跑步日（0=週一, 6=週日）。
        strength_days: 每週重訓日。
        protein_target_g: 每日蛋白質目標（g）。
        water_target_l: 每日飲水目標（L）。
    """

    # Synthetic demonstration values, never an actual person's health record.
    # Deployment requires a privately configured, reviewed athlete profile.
    weight_kg: float = 70.0
    target_weight_kg: float = 65.0
    vdot: float = 38.0
    max_hr: int = 185
    glp1_injection_day: int = 4  # Friday
    glp1_severity: str = "minimal"  # minimal/mild/moderate/severe
    easy_pace_min: float = 6.5
    easy_pace_max: float = 7.0
    run_days: Tuple[int, ...] = (1, 3, 5)  # Tue, Thu, Sat
    strength_days: Tuple[int, ...] = (0, 2)  # Mon, Wed
    protein_target_g: int = 160
    water_target_l: float = 3.0


# ── 規則引擎閾值設定 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuleConfig:
    """規則引擎可調閾值。

    所有閾值都有合理預設值，可透過環境變數或設定檔覆蓋。

    Attributes:
        body_battery_rest: Body Battery 低於此值建議完全休息。
        readiness_rest: Training Readiness 低於此值建議休息。
        readiness_moderate: Training Readiness 高於此值可進行高強度。
        sleep_downgrade_hours: 睡眠低於此時數，降級訓練強度。
        sleep_debt_downgrade_hours: 7 日累積睡眠債（小時）高於此值，降級訓練
            強度（睡眠一級管理，T18）——即使昨晚單獨一晚不算太差，長期累積
            的債務仍會被抓到，而不是只看昨晚一晚。
        sleep_acute_downgrade_hours: 昨晚睡眠低於此時數，無論 7 日債務多寡都
            直接降級（睡眠一級管理，T18）——比 ``sleep_downgrade_hours`` 更
            嚴重的單晚急性睡眠剝奪門檻。
        acwr_overtraining: ACWR 高於此值，過度訓練風險。
        acwr_undertraining: ACWR 低於此值，可增加訓練量。
        hrv_deficit_pct: HRV 低於基線此百分比，視為低 HRV。
        glp1_cooldown_hours: GLP-1 注射後避免高強度的小時數。
        stress_warning: 壓力值高於此值，發出警告。
        recovery_warning_hours: 恢復時間超過此值，發出警告。
    """

    body_battery_rest: int = 30
    readiness_rest: int = 40
    readiness_moderate: int = 60
    sleep_downgrade_hours: float = 6.0
    sleep_debt_downgrade_hours: float = 6.0
    sleep_acute_downgrade_hours: float = 4.5
    acwr_overtraining: float = 1.3
    acwr_undertraining: float = 0.8
    hrv_deficit_pct: float = 0.15
    glp1_cooldown_hours: int = 48
    stress_warning: int = 60
    recovery_warning_hours: int = 48


# ── 規則引擎輸出 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuleVerdict:
    """規則引擎的評估結果。

    Attributes:
        readiness_level: 建議強度等級（REST / EASY / MODERATE / HIGH）。
        recommended_intensity: 建議訓練類型描述。
        max_hr_zone: 建議最高心率區間（1–5）。
        warnings: 警告訊息（不可變元組）。
        flags: 標記（如 ``'glp1_window'``、``'can_increase_load'``）。
        raw_scores: 原始分數（readiness、hrv_ratio、acwr 等）。
    """

    readiness_level: str = "MODERATE"
    recommended_intensity: str = ""
    max_hr_zone: int = 3
    warnings: Tuple[str, ...] = ()
    flags: Tuple[str, ...] = ()
    raw_scores: Tuple[Tuple[str, float], ...] = ()


# ── AI 教練輸出 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrainingPrescription:
    """AI 教練產生的每日訓練處方。

    Attributes:
        date: 日期（ISO 格式）。
        session_type: 訓練類型（LSD/Tempo/Interval/Recovery/Strength/Rest）。
        description: 訓練描述摘要。
        target_distance_km: 目標距離（km）。
        target_pace: 目標配速（如 ``'6:30-7:00'``）。
        target_hr_zone: 目標心率區間（1–5）。
        warmup: 暖身描述。
        main_set: 主訓練內容。
        cooldown: 收操描述。
        strength_notes: 重訓備註（重訓日用）。
        nutrition_notes: 營養提醒。
        warnings: 注意事項。
        confidence: AI 信心等級（HIGH/MEDIUM/LOW）。
        confidence_reason: 信心等級原因。
    """

    date: str = ""
    session_type: str = ""
    description: str = ""
    target_distance_km: Optional[float] = None
    target_pace: Optional[str] = None
    target_hr_zone: Optional[int] = None
    warmup: str = ""
    main_set: str = ""
    cooldown: str = ""
    strength_notes: Optional[str] = None
    nutrition_notes: Optional[str] = None
    warnings: Tuple[str, ...] = ()
    confidence: str = "MEDIUM"
    confidence_reason: str = ""


# ── 歷史上下文 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WeeklyContext:
    """過去一段時間的訓練、健康、營養匯總，作為 AI 教練的上下文輸入。

    Attributes:
        days: 涵蓋天數。
        health_records: 健康紀錄（不可變元組）。
        activity_records: 活動紀錄（不可變元組）。
        nutrition_records: 營養紀錄（不可變元組）。
        total_distance_km: 總跑步距離（km）。
        total_training_load: 總訓練負荷（EPOC）。
        run_count: 跑步次數。
        strength_count: 重訓次數。
        avg_sleep_score: 平均睡眠分數。
        avg_hrv: 平均 HRV。
        avg_resting_hr: 平均安靜心率。
        chronic_load: 慢性訓練負荷（28 天均值）。
        avg_daily_calories: 平均每日攝取熱量（kcal）。
        avg_daily_protein_g: 平均每日蛋白質（g）。
        avg_daily_carbs_g: 平均每日碳水（g）。
        avg_daily_fat_g: 平均每日脂肪（g）。
        avg_daily_water_ml: 平均每日飲水量（ml）。
        latest_weight_kg: 最近一次體重（kg）。
        latest_body_fat_pct: 最近一次體脂率（%）。
        weight_trend_kg: 期間內體重變化（kg，正為增加，負為減少）。
    """

    days: int = 7
    health_records: Tuple[HealthData, ...] = ()
    activity_records: Tuple[ActivityData, ...] = ()
    nutrition_records: Tuple["NutritionData", ...] = ()
    total_distance_km: float = 0.0
    total_training_load: float = 0.0
    run_count: int = 0
    strength_count: int = 0
    avg_sleep_score: Optional[float] = None
    avg_hrv: Optional[float] = None
    avg_resting_hr: Optional[float] = None
    chronic_load: Optional[float] = None
    avg_daily_calories: Optional[float] = None
    avg_daily_protein_g: Optional[float] = None
    avg_daily_carbs_g: Optional[float] = None
    avg_daily_fat_g: Optional[float] = None
    avg_daily_water_ml: Optional[float] = None
    latest_weight_kg: Optional[float] = None
    latest_body_fat_pct: Optional[float] = None
    weight_trend_kg: Optional[float] = None
