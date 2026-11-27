"""scripts/training_advisor.py — 馬拉松週訓練 delta-advisor。

架構：Garmin Coach 為唯一權威課表，AI 只做「修正建議（delta）」，
絕不生成平行課表（T12：消除「兩份權威課表」問題）。

課表來源（優先序）：

1. **自動**：Garmin Coach 自適應計劃 API（`get_adaptive_training_plan_by_id`；
   不可靠，常被 429 擋 — 失敗時自動降級）。
2. **手動備援**：`coach_week.json`（repo 根目錄或 ``logs/``），每週日花 30 秒
   從手錶/App 抄一次。格式：7 個物件的 JSON 陣列 ``{"day", "type", "target"}``。
   檔案缺失或過期（mtime > 7 天）視為無課表。
3. **皆無** → 純 DB 諮詢模式：只讀 Health/Activity/Nutrition 給身體狀態評估，
   明確聲明未讀到課表，不編造課表。

使用方式::

    # 乾跑模式（不發送 Telegram，印出結果）
    python scripts/training_advisor.py --dry-run

    # 正常執行（發送 Telegram）
    python scripts/training_advisor.py --telegram

    # 指定回顧天數
    python scripts/training_advisor.py --dry-run --days 14
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_glp1_settings, get_vdot
from models import ActivityData, AthleteProfile, HealthData, NutritionData, WeeklyContext
from notion_client_helper import get_notion_client
from scripts.notion_reader import (
    build_weekly_context,
    fetch_activity_history,
    fetch_health_history,
    fetch_nutrition_history,
    rolling_resting_hr,
)
from utils.delivery import send_telegram_text
from utils.fueling import carb_target_g_per_hr, format_fuel_history, parse_workout_text
from utils.logger import get_logger
from utils.run_ledger import append_run
from utils.vdot_paces import DEFAULT_RESTING_HR, karvonen_zones, vdot_to_paces

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_GARMIN_TOKEN_DIR = str(Path(__file__).resolve().parent.parent / ".garmin_tokens")


def _parse_iso_date(raw: object) -> Optional[date]:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _get_race_date() -> Optional[date]:
    return _parse_iso_date(os.getenv("RACE_DATE", ""))


# 日期名稱對照
_DAY_NAMES = {0: "週一", 1: "週二", 2: "週三", 3: "週四", 4: "週五", 5: "週六", 6: "週日"}

# Garmin Coach 訓練期別中英對照
_PHASE_NAMES = {
    "TRANSITION": "過渡期",
    "BASE": "基礎期",
    "BUILD": "建量期",
    "PEAK": "巔峰期",
    "TAPER": "減量期",
    "TARGET_EVENT_DAY": "比賽日",
}


# ── Garmin Coach 資料結構 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GarminCoachTask:
    """Garmin Coach 單日排程。"""
    calendar_date: str
    workout_name: Optional[str]
    workout_description: Optional[str]
    training_effect: Optional[str]
    estimated_distance_m: Optional[float]
    estimated_duration_s: Optional[float]
    is_rest_day: bool
    status: Optional[str]


@dataclass(frozen=True)
class GarminTrainingMetrics:
    """Garmin 訓練指標（Training Readiness + Load Balance + Status）。"""
    # Training Readiness
    readiness_score: Optional[int] = None
    readiness_level: Optional[str] = None  # LOW / MODERATE / HIGH
    hrv_weekly_avg: Optional[int] = None
    hrv_feedback: Optional[str] = None
    sleep_history_feedback: Optional[str] = None
    recovery_time_min: Optional[int] = None
    acute_load: Optional[int] = None
    # Training Load Balance
    low_aerobic_load: Optional[float] = None
    low_aerobic_target_min: Optional[int] = None
    low_aerobic_target_max: Optional[int] = None
    high_aerobic_load: Optional[float] = None
    high_aerobic_target_min: Optional[int] = None
    high_aerobic_target_max: Optional[int] = None
    anaerobic_load: Optional[float] = None
    anaerobic_target_min: Optional[int] = None
    anaerobic_target_max: Optional[int] = None
    balance_feedback: Optional[str] = None  # BALANCED / LOW_AEROBIC_DEFICIT / etc.
    # Training Status
    training_status_phrase: Optional[str] = None  # PRODUCTIVE / MAINTAINING / DETRAINING
    vo2_max: Optional[float] = None
    acwr_ratio: Optional[float] = None
    acwr_status: Optional[str] = None  # OPTIMAL / HIGH / LOW


@dataclass(frozen=True)
class GarminCoachContext:
    """Garmin Coach 本週上下文 + 訓練指標。"""
    plan_name: str
    current_phase: str
    current_phase_cn: str
    weeks_total: int
    race_date: str
    tasks: Tuple[GarminCoachTask, ...]
    metrics: Optional[GarminTrainingMetrics] = None


def _race_days(coach: Optional[GarminCoachContext]) -> Optional[int]:
    if coach is None:
        return None
    if coach.current_phase != "MANUAL":
        race_date = _parse_iso_date(coach.race_date)
    else:
        race_date = _get_race_date()
    if race_date is None:
        return None
    remaining = (race_date - date.today()).days
    return remaining if remaining >= 0 else None


def _race_countdown(days: Optional[int]) -> str:
    if days is None:
        return ""
    if days == 0:
        return "今天是目標賽事日"
    return f"距目標賽事 {days} 天"


def _fuel_target(coach: Optional[GarminCoachContext]) -> Optional[Tuple[int, int]]:
    """只有有效目標賽事及明確至少 90 分鐘的課表，才計算賽前補給目標。"""
    days = _race_days(coach)
    if not coach or days is None:
        return None
    for task in coach.tasks:
        if task.is_rest_day:
            continue
        duration = task.estimated_duration_s
        if duration is None:
            minutes, _ = parse_workout_text(task.workout_description)
            duration = minutes * 60 if minutes is not None else 0
        if duration >= 90 * 60:
            return carb_target_g_per_hr(days // 7)
    return None


def _phase_suffix(coach: GarminCoachContext) -> str:
    return f" — {coach.current_phase_cn}" if coach.current_phase in _PHASE_NAMES else ""


# ── Garmin 訓練指標讀取 ─────────────────────────────────────────────────────


def _fetch_garmin_training_metrics(client: "Garmin") -> Optional[GarminTrainingMetrics]:
    """從 Garmin Connect 讀取 Training Readiness + Load Balance + Status。"""
    from datetime import timedelta as td

    yesterday = (date.today() - td(days=1)).isoformat()

    readiness_score = None
    readiness_level = None
    hrv_weekly_avg = None
    hrv_feedback = None
    sleep_history_feedback = None
    recovery_time_min = None
    acute_load_val = None

    # Training Readiness（用昨天的資料，今天可能還沒同步）
    try:
        tr_data = client.get_training_readiness(yesterday)
        if tr_data and isinstance(tr_data, list) and len(tr_data) > 0:
            latest = tr_data[0]  # 最新一筆
            readiness_score = latest.get("score")
            readiness_level = latest.get("level")
            hrv_weekly_avg = latest.get("hrvWeeklyAverage")
            hrv_feedback = latest.get("hrvFactorFeedback")
            sleep_history_feedback = latest.get("sleepHistoryFactorFeedback")
            recovery_time_min = latest.get("recoveryTime")
            acute_load_val = latest.get("acuteLoad")
            logger.info("Training Readiness: %s (%s)", readiness_score, readiness_level)
    except Exception as exc:
        logger.warning("Training Readiness 讀取失敗：%s", exc)

    # Training Status + Load Balance
    low_aero = high_aero = anaerobic = None
    low_min = low_max = high_min = high_max = ana_min = ana_max = None
    balance_fb = None
    status_phrase = None
    vo2_max = None
    acwr_ratio = None
    acwr_status = None

    try:
        ts_data = client.get_training_status(yesterday)
        if ts_data:
            # VO2 Max
            v = ts_data.get("mostRecentVO2Max") or {}
            generic = v.get("generic") or {}
            vo2_max = generic.get("vo2MaxPreciseValue")

            # Load Balance
            lb = ts_data.get("mostRecentTrainingLoadBalance") or {}
            lb_map = lb.get("metricsTrainingLoadBalanceDTOMap") or {}
            for device_data in lb_map.values():
                low_aero = device_data.get("monthlyLoadAerobicLow")
                low_min = device_data.get("monthlyLoadAerobicLowTargetMin")
                low_max = device_data.get("monthlyLoadAerobicLowTargetMax")
                high_aero = device_data.get("monthlyLoadAerobicHigh")
                high_min = device_data.get("monthlyLoadAerobicHighTargetMin")
                high_max = device_data.get("monthlyLoadAerobicHighTargetMax")
                anaerobic = device_data.get("monthlyLoadAnaerobic")
                ana_min = device_data.get("monthlyLoadAnaerobicTargetMin")
                ana_max = device_data.get("monthlyLoadAnaerobicTargetMax")
                balance_fb = device_data.get("trainingBalanceFeedbackPhrase")
                break  # 取第一個裝置

            # Training Status
            mts = ts_data.get("mostRecentTrainingStatus") or {}
            latest_ts = mts.get("latestTrainingStatusData") or {}
            for device_ts in latest_ts.values():
                status_phrase = device_ts.get("trainingStatusFeedbackPhrase")
                acuteDTO = device_ts.get("acuteTrainingLoadDTO") or {}
                acwr_ratio = acuteDTO.get("dailyAcuteChronicWorkloadRatio")
                acwr_status = acuteDTO.get("acwrStatus")
                break

            logger.info(
                "Training Status: %s | VO2Max: %s | Balance: %s",
                status_phrase, vo2_max, balance_fb,
            )
    except Exception as exc:
        logger.warning("Training Status 讀取失敗：%s", exc)

    return GarminTrainingMetrics(
        readiness_score=readiness_score,
        readiness_level=readiness_level,
        hrv_weekly_avg=hrv_weekly_avg,
        hrv_feedback=hrv_feedback,
        sleep_history_feedback=sleep_history_feedback,
        recovery_time_min=recovery_time_min,
        acute_load=acute_load_val,
        low_aerobic_load=low_aero,
        low_aerobic_target_min=low_min,
        low_aerobic_target_max=low_max,
        high_aerobic_load=high_aero,
        high_aerobic_target_min=high_min,
        high_aerobic_target_max=high_max,
        anaerobic_load=anaerobic,
        anaerobic_target_min=ana_min,
        anaerobic_target_max=ana_max,
        balance_feedback=balance_fb,
        training_status_phrase=status_phrase,
        vo2_max=vo2_max,
        acwr_ratio=acwr_ratio,
        acwr_status=acwr_status,
    )


# ── Garmin Coach 讀取 ──────────────────────────────────────────────────────


def _fetch_garmin_coach_context() -> Optional[GarminCoachContext]:
    """從 Garmin Connect 讀取 Garmin Coach 自適應訓練計劃。

    Returns:
        GarminCoachContext 包含當前期別和本週排程；讀取失敗時回傳 None。
    """
    plan_id = os.getenv("GARMIN_COACH_PLAN_ID", "")
    if not plan_id:
        logger.info("GARMIN_COACH_PLAN_ID 未設定，跳過 Garmin Coach")
        return None

    garmin_email = os.getenv("GARMIN_EMAIL", "")
    garmin_password = os.getenv("GARMIN_PASSWORD", "")
    if not garmin_email or not garmin_password:
        logger.warning("GARMIN_EMAIL/PASSWORD 未設定，跳過 Garmin Coach")
        return None

    try:
        from garminconnect import Garmin

        Path(_GARMIN_TOKEN_DIR).mkdir(exist_ok=True)
        client = Garmin(garmin_email, garmin_password)
        client.login(tokenstore=_GARMIN_TOKEN_DIR)
        logger.info("Garmin Connect 登入成功")

        detail = client.get_adaptive_training_plan_by_id(int(plan_id))
    except Exception as exc:
        logger.warning("Garmin Coach 讀取失敗（降級模式）：%s", type(exc).__name__)
        return None

    if not isinstance(detail, dict):
        logger.warning("Garmin Coach 未回傳可用的計畫資料")
        return None

    today = date.today()
    end_date = _parse_iso_date(detail.get("endDate"))
    if end_date is not None and end_date < today:
        logger.info("Garmin Coach 指定計畫已結束（%s），不採用舊課表", end_date)
        return None

    # 解析訓練期別
    current_phase = "UNKNOWN"
    phases = detail.get("adaptivePlanPhases") or []
    for phase in phases:
        if isinstance(phase, dict) and phase.get("currentPhase"):
            phase_name = phase.get("trainingPhase")
            if isinstance(phase_name, str):
                current_phase = phase_name
            break

    # 解析本週排程（taskList 只包含近期 ~7 天）
    tasks: List[GarminCoachTask] = []
    for task in detail.get("taskList") or []:
        if not isinstance(task, dict):
            continue
        task_date = _parse_iso_date(task.get("calendarDate"))
        if task_date is None or not today <= task_date <= today + timedelta(days=7):
            continue
        tw = task.get("taskWorkout") or {}
        if not isinstance(tw, dict) or not any(
            tw.get(key) for key in ("workoutName", "trainingEffectLabel", "restDay")
        ):
            continue
        tasks.append(GarminCoachTask(
            calendar_date=task_date.isoformat(),
            workout_name=tw.get("workoutName"),
            workout_description=tw.get("workoutDescription"),
            training_effect=tw.get("trainingEffectLabel"),
            estimated_distance_m=tw.get("estimatedDistanceInMeters"),
            estimated_duration_s=tw.get("estimatedDurationInSecs"),
            is_rest_day=tw.get("restDay", False),
            status=tw.get("adaptiveCoachingWorkoutStatus"),
        ))

    if not tasks:
        logger.info("Garmin Coach 已回傳計畫，但沒有可用的近期課表")
        return None

    coach = GarminCoachContext(
        plan_name=detail.get("name", "Garmin Coach"),
        current_phase=current_phase,
        current_phase_cn=_PHASE_NAMES.get(current_phase, current_phase),
        weeks_total=detail.get("durationInWeeks", 0),
        race_date=detail.get("endDate", ""),
        tasks=tuple(tasks),
        metrics=_fetch_garmin_training_metrics(client),
    )

    logger.info(
        "Garmin Coach：%s | %s（%s）| 本週 %d 個排程",
        coach.plan_name, coach.current_phase_cn,
        coach.current_phase, len(coach.tasks),
    )
    return coach


# ── 手動課表備援（coach_week.json）────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COACH_WEEK_FILENAME = "coach_week.json"
_COACH_WEEK_MAX_AGE_DAYS = 7
_REST_TYPE_KEYWORDS = frozenset({"rest", "off", "休息", "休息日"})


def _default_coach_week_paths() -> List[Path]:
    """coach_week.json 的預設搜尋路徑（repo 根目錄優先，其次 logs/）。"""
    return [
        _REPO_ROOT / _COACH_WEEK_FILENAME,
        _REPO_ROOT / "logs" / _COACH_WEEK_FILENAME,
    ]


def _load_manual_coach_week(
    paths: Optional[List[Path]] = None,
) -> Optional[GarminCoachContext]:
    """讀取手動抄錄的 Garmin Coach 週課表（coach_week.json）。

    Garmin Coach API 不可靠（常被 429 擋），此為人工 30 秒的備援通道：
    使用者每週日從手錶/App 抄一次本週課表。

    格式：JSON 陣列，每筆 ``{"day": ..., "type": ..., "target": ...}``
    （``day`` 可為 ISO 日期或星期名稱；``target`` 可省略）。

    Args:
        paths: 搜尋路徑清單（預設 repo 根目錄與 logs/，供測試注入）。

    Returns:
        GarminCoachContext（tasks 由 JSON 轉換），以下情況一律回傳 None：
        檔案缺失、mtime 超過 7 天（過期）、JSON 解析失敗、格式不符。
    """
    candidates = paths if paths is not None else _default_coach_week_paths()
    path: Optional[Path] = next((p for p in candidates if p.is_file()), None)
    if path is None:
        logger.info("找不到 %s（手動課表未提供）", _COACH_WEEK_FILENAME)
        return None

    # ── 過期檢查（>7 天視為無課表，避免用上週課表誤導本週建議）──────────
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError as exc:
        logger.warning("%s 無法讀取檔案時間：%s", path, exc)
        return None
    age = datetime.now() - mtime
    if age > timedelta(days=_COACH_WEEK_MAX_AGE_DAYS):
        logger.warning(
            "%s 已過期（%.1f 天前更新，> %d 天）— 視為無課表，請重抄本週課表",
            path, age.total_seconds() / 86400, _COACH_WEEK_MAX_AGE_DAYS,
        )
        return None

    # ── 解析與驗證 ────────────────────────────────────────────────────────
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("%s 解析失敗（視為無課表）：%s", path, exc)
        return None

    if not isinstance(raw, list) or not raw:
        logger.warning("%s 格式不符（需為非空 JSON 陣列）— 視為無課表", path)
        return None

    tasks: List[GarminCoachTask] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not str(item.get("day", "")).strip() \
                or not str(item.get("type", "")).strip():
            logger.warning(
                "%s 第 %d 筆缺少 day/type（需 {day, type, target}）— 視為無課表",
                path, i + 1,
            )
            return None
        day = str(item["day"]).strip()
        workout_type = str(item["type"]).strip()
        target_raw = item.get("target")
        target = str(target_raw).strip() if target_raw is not None else None
        is_rest = workout_type.lower() in _REST_TYPE_KEYWORDS
        tasks.append(GarminCoachTask(
            calendar_date=day,
            workout_name=None if is_rest else workout_type,
            workout_description=target or None,
            training_effect=None,
            estimated_distance_m=None,
            estimated_duration_s=None,
            is_rest_day=is_rest,
            status=None,
        ))

    if len(tasks) != 7:
        logger.warning("%s 有 %d 筆（預期 7 筆）— 仍採用，請確認是否漏抄", path, len(tasks))

    logger.info("使用手動課表 %s（%d 筆，%s 更新）", path, len(tasks), mtime.date())
    return GarminCoachContext(
        plan_name=f"手動抄錄（{_COACH_WEEK_FILENAME}）",
        current_phase="MANUAL",
        current_phase_cn="手動抄錄課表",
        weeks_total=0,
        race_date="",
        tasks=tuple(tasks),
        metrics=None,
    )


def _format_garmin_coach_for_prompt(coach: GarminCoachContext) -> str:
    """將 Garmin Coach 排程 + 訓練指標格式化為 prompt 用文字。"""
    lines = [
        f"## Garmin Coach 計劃：{coach.plan_name}",
    ]
    if _phase_suffix(coach):
        lines.append(f"- 當前訓練期別：{coach.current_phase_cn}（{coach.current_phase}）")
    if coach.weeks_total > 0:
        lines.append(f"- 計劃總週數：{coach.weeks_total} 週")
    if _race_days(coach) is not None and coach.race_date:
        lines.append(f"- 目標賽事日期：{coach.race_date}")

    # ── 訓練指標摘要 ─────────────────────────────────────────────────
    m = coach.metrics
    if m:
        lines.append("")
        lines.append("### Garmin 訓練指標：")

        if m.readiness_score is not None:
            lines.append(
                f"- Training Readiness: {m.readiness_score}/100 ({m.readiness_level})"
                f" — 綜合評估你今天是否適合高強度訓練"
            )
        if m.hrv_weekly_avg is not None:
            lines.append(
                f"- HRV 週均值: {m.hrv_weekly_avg}ms ({m.hrv_feedback})"
                f" — 自律神經恢復狀態，越高越好"
            )
        if m.sleep_history_feedback:
            lines.append(
                f"- 睡眠歷史: {m.sleep_history_feedback}"
                f" — 近期睡眠品質趨勢"
            )
        if m.recovery_time_min is not None:
            lines.append(
                f"- 恢復時間: {m.recovery_time_min} 分鐘"
                f" — 預計完全恢復所需時間"
            )
        if m.vo2_max is not None:
            lines.append(f"- VO2 Max: {m.vo2_max}")
        if m.training_status_phrase:
            lines.append(
                f"- 訓練狀態: {m.training_status_phrase}"
                f" — PRODUCTIVE=高效 / MAINTAINING=維持 / DETRAINING=退步"
            )
        if m.acwr_ratio is not None:
            lines.append(
                f"- ACWR: {m.acwr_ratio} ({m.acwr_status})"
                f" — 急慢性負荷比，0.8-1.3 為最佳，>1.5 過度訓練風險"
            )

        # Training Load Balance — 高/低有氧 + 無氧
        if m.low_aerobic_load is not None:
            def _load_status(val: Optional[float], lo: Optional[int], hi: Optional[int]) -> str:
                if val is None or lo is None or hi is None:
                    return "?"
                if val < lo:
                    return "偏低，需增加"
                elif val > hi:
                    return "偏高，需減少"
                return "在範圍內"

            lines.append("")
            lines.append("### 訓練負荷平衡（月度累積）：")
            lines.append(
                f"- 低強度有氧: {m.low_aerobic_load:.0f} / 目標 {m.low_aerobic_target_min}-{m.low_aerobic_target_max}"
                f" → {_load_status(m.low_aerobic_load, m.low_aerobic_target_min, m.low_aerobic_target_max)}"
                f" — 輕鬆跑、恢復跑（Z1-Z2）"
            )
            lines.append(
                f"- 高強度有氧: {m.high_aerobic_load:.0f} / 目標 {m.high_aerobic_target_min}-{m.high_aerobic_target_max}"
                f" → {_load_status(m.high_aerobic_load, m.high_aerobic_target_min, m.high_aerobic_target_max)}"
                f" — 節奏跑、LSD、乳酸閾值訓練（Z3-Z4）"
            )
            lines.append(
                f"- 無氧: {m.anaerobic_load:.0f} / 目標 {m.anaerobic_target_min}-{m.anaerobic_target_max}"
                f" → {_load_status(m.anaerobic_load, m.anaerobic_target_min, m.anaerobic_target_max)}"
                f" — 間歇跑、衝刺、高強度重訓（Z5+）"
            )
            lines.append(f"- 整體平衡: {m.balance_feedback}")

    lines.append("")
    lines.append("### 本週排程：")

    for task in coach.tasks:
        d = task.calendar_date
        try:
            weekday = _DAY_NAMES.get(date.fromisoformat(d).weekday(), "")
        except ValueError:
            weekday = ""
        # 手動課表的 day 可能是「週一」這類非 ISO 標籤 → 不加空括號
        label = f"{d}（{weekday}）" if weekday else d

        if task.is_rest_day:
            lines.append(f"- {label}：😴 休息日")
        elif task.workout_name:
            dist = ""
            if task.estimated_distance_m:
                dist = f" {task.estimated_distance_m / 1000:.1f}km"
            dur = ""
            if task.estimated_duration_s:
                dur = f" {task.estimated_duration_s / 60:.0f}min"
            hr = ""
            if task.workout_description:
                hr = f" ({task.workout_description})"
            effect = f" [{task.training_effect}]" if task.training_effect else ""
            lines.append(f"- {label}：{task.workout_name}{dist}{dur}{hr}{effect}")
        else:
            phrase = task.training_effect or "EVENT"
            lines.append(f"- {label}：{phrase}")

    return "\n".join(lines)


# ── 公開介面 ─────────────────────────────────────────────────────────────────


def run_weekly_advisor(
    dry_run: bool = False,
    days: int = 28,
    output_telegram: bool = False,
) -> Optional[str]:
    """執行每週訓練建議流程。

    Args:
        dry_run: 乾跑模式，只印出結果不發送 Telegram。
        days: 歷史數據回溯天數（預設 28 天）。
        output_telegram: 是否推送至 Telegram（dry_run 為 True 時無效）。
            關閉時只生成文字，不更新正式執行帳本或發送告警。

    Returns:
        格式化的週訓練計劃文字，失敗時回傳 None。
    """
    logger.info("=" * 60)
    logger.info("週訓練建議 — %s", date.today().isoformat())
    logger.info("=" * 60)
    production_delivery = output_telegram and not dry_run

    # ── 載入設定 ──────────────────────────────────────────────────────────────
    notion_api_key = os.getenv("NOTION_API_KEY", "")
    health_db_id = os.getenv("HEALTH_DB_ID", "")
    activity_db_id = os.getenv("ACTIVITY_DB_ID", "")
    nutrition_db_id = os.getenv("NUTRITION_DB_ID", "")
    # 商業版 workspace 用的獨立 API key（如果 Nutrition / Activity 在不同 workspace）
    nutrition_api_key = os.getenv("NUTRITION_NOTION_API_KEY", notion_api_key)
    activity_api_key = os.getenv("ACTIVITY_NOTION_API_KEY", notion_api_key)

    if not notion_api_key:
        logger.error("NOTION_API_KEY 未設定，無法繼續")
        if production_delivery:
            append_run("training_advisor.py", ok=False, wrote_notion=False)
        return None

    if not health_db_id:
        logger.error("HEALTH_DB_ID 未設定，無法繼續")
        if production_delivery:
            append_run("training_advisor.py", ok=False, wrote_notion=False)
        return None

    if not activity_db_id:
        logger.warning("ACTIVITY_DB_ID 未設定，活動歷史將為空（降級模式）")

    # ── 讀取 Garmin Coach 計劃（自動 API 優先，coach_week.json 手動備援）──────
    logger.info("讀取 Garmin Coach 計劃...")
    coach_context: Optional[GarminCoachContext] = _fetch_garmin_coach_context()
    if coach_context and coach_context.tasks:
        logger.info("Garmin Coach：採用 %d 筆排程", len(coach_context.tasks))
    else:
        logger.info("沒有可用的 Garmin Coach 課表，改找手動課表 %s...", _COACH_WEEK_FILENAME)
        coach_context = _load_manual_coach_week()
        if coach_context:
            logger.info("✅ 手動課表：%d 筆排程", len(coach_context.tasks))
        else:
            logger.info("⚠️ 無 Coach 課表（API + 手動皆不可用）→ 純身體狀態諮詢模式，不生成課表")

    # ── 讀取 Notion 歷史數據（支援多 workspace）────────────────────────────────
    client = get_notion_client(notion_api_key)

    logger.info("讀取健康歷史（%d 天）...", days)
    health_records: List[HealthData] = fetch_health_history(client, health_db_id, days=days)

    activity_records: List[ActivityData] = []
    if activity_db_id:
        logger.info("讀取活動歷史（%d 天）...", days)
        activity_client = get_notion_client(activity_api_key) if activity_api_key != notion_api_key else client
        activity_records = fetch_activity_history(activity_client, activity_db_id, days=days)
    else:
        logger.info("跳過活動歷史讀取（ACTIVITY_DB_ID 未設定）")

    nutrition_records: List[NutritionData] = []
    if nutrition_db_id:
        logger.info("讀取營養歷史（%d 天）...", days)
        nutrition_client = get_notion_client(nutrition_api_key) if nutrition_api_key != notion_api_key else client
        nutrition_records = fetch_nutrition_history(nutrition_client, nutrition_db_id, days=days)
    else:
        logger.info("跳過營養歷史讀取（NUTRITION_DB_ID 未設定）")

    # ── 建構訓練上下文 ────────────────────────────────────────────────────────
    context: WeeklyContext = build_weekly_context(
        health_records,
        activity_records,
        nutrition_records=nutrition_records,
        days=days,
    )
    logger.info(
        "訓練上下文：查詢 %d 天 | 健康 %d 筆 | 活動 %d 筆 | 營養 %d 筆",
        days, len(health_records), len(activity_records), len(nutrition_records),
    )

    # ── 載入運動員檔案 ────────────────────────────────────────────────────────
    profile: AthleteProfile = _load_athlete_profile()

    # ── 近 7 天安靜心率（T13：Karvonen 心率區間用，獨立於 context 的整體
    # 回顧天數，一律取最近 7 筆，取不到時讓 _generate_weekly_plan 自行
    # 退回 context.avg_resting_hr / DEFAULT_RESTING_HR）───────────────────
    resting_hr_7d = rolling_resting_hr(health_records)

    # ── 生成週訓練計劃 ────────────────────────────────────────────────────────
    logger.info("生成週訓練計劃...")
    plan_text = _generate_weekly_plan(context, profile, coach_context, resting_hr=resting_hr_7d)

    if not plan_text:
        logger.warning("AI 未產生週建議，使用備援訊息")
        plan_text = _build_fallback_plan(context, coach_context)

    # ── 輸出 ──────────────────────────────────────────────────────────────────
    if dry_run:
        logger.info("乾跑模式 — 計劃如下：")
        # Windows console 可能不支援 emoji，用 utf-8 強制輸出
        sys.stdout.buffer.write(("\n" + plan_text + "\n").encode("utf-8"))
        return plan_text
    if not output_telegram:
        return plan_text
    if not _send_to_telegram(plan_text):
        append_run("training_advisor.py", ok=False, wrote_notion=False)
        return None

    # training_advisor 本身不寫 Notion（只讀歷史、生成建議推 Telegram），
    # wrote_notion 恆為 False。
    append_run("training_advisor.py", ok=True, wrote_notion=False)

    return plan_text


# ── 運動員檔案 ───────────────────────────────────────────────────────────────


def _load_athlete_profile() -> AthleteProfile:
    """載入運動員檔案。

    VDOT 優先讀取 ``.env`` 的 ``VDOT``（單一來源 ``config.get_vdot``），
    使用者每月依最近比賽/測驗結果手動更新即可；未設定時退回預設 38.0。
    輕鬆跑配速表隨 VDOT 動態推導（``utils.vdot_paces.vdot_to_paces``），
    不再寫死 6.5/7.0（T13）。其餘欄位仍是目前硬編碼（未來可改為 JSON/env）。
    """
    glp1_settings = get_glp1_settings()
    injection_day = glp1_settings.injection_day
    glp1_severity = glp1_settings.severity
    vdot = get_vdot()
    paces = vdot_to_paces(vdot)
    return AthleteProfile(
        weight_kg=float(os.getenv("ATHLETE_WEIGHT_KG", "70.0")),
        target_weight_kg=float(os.getenv("ATHLETE_TARGET_WEIGHT_KG", "65.0")),
        vdot=vdot,
        max_hr=185,
        glp1_injection_day=injection_day,
        glp1_severity=glp1_severity,
        easy_pace_min=paces.easy_min,
        easy_pace_max=paces.easy_max,
        run_days=(1, 3, 5),
        strength_days=(0, 2),
        protein_target_g=160,
        water_target_l=3.0,
    )


# ── Claude API 週計劃生成 ─────────────────────────────────────────────────────


def _generate_weekly_plan(
    context: WeeklyContext,
    profile: AthleteProfile,
    coach: Optional[GarminCoachContext] = None,
    resting_hr: Optional[float] = None,
) -> Optional[str]:
    """呼叫 AI API 生成週訓練修正建議。

    若有 Garmin Coach context，AI 角色為「微調 Coach 建議」；
    否則 AI 獨立生成 7 天計劃。

    Args:
        context: 近期訓練上下文（Health DB + Activity DB）。
        profile: 運動員檔案。
        coach: Garmin Coach 本週排程（可選）。
        resting_hr: 安靜心率（bpm），供 Karvonen 心率區間計算用（T13）。
            優先序：本參數（``run_weekly_advisor`` 傳入的近 7 天實測均值）
            → ``context.avg_resting_hr`` → ``DEFAULT_RESTING_HR``。

    Returns:
        格式化的計劃文字（Markdown），失敗時回傳 None。
    """
    from scripts.ai_coach import _call_ai_api

    coach = coach if coach and coach.tasks else None
    race_countdown = _race_countdown(_race_days(coach))
    weekly_summary = _format_context_for_prompt(context)

    # ── 賽中補給演練（T11）：本週目標 + 最近 /fuel 回報 ────────────────
    fuel_target = _fuel_target(coach)

    # ── 配速表與心率區間（T13）：由 VDOT／(max_hr, 安靜心率) 動態推導，
    # 取代原本寫死的數字。安靜心率優先使用近期實測滾動均值
    # （WeeklyContext.avg_resting_hr，來自 fetch_health_history），
    # 取不到時退回 DEFAULT_RESTING_HR。
    paces = vdot_to_paces(profile.vdot)
    if resting_hr is None:
        resting_hr = context.avg_resting_hr if context.avg_resting_hr is not None else DEFAULT_RESTING_HR
    hr_zones = karvonen_zones(profile.max_hr, resting_hr)
    pace_zone_section = (
        f"- 配速表（VDOT {profile.vdot}）：E {_format_pace(paces.easy_min)}-{_format_pace(paces.easy_max)} "
        f"/ M {_format_pace(paces.marathon)} / T {_format_pace(paces.threshold)} "
        f"/ I {_format_pace(paces.interval)} / R {_format_pace(paces.repetition)} min/km\n"
        f"- 心率區間（Karvonen，安靜心率 {resting_hr}bpm）："
        f"Z1<{hr_zones.z1_max} / Z2 {hr_zones.z1_max}-{hr_zones.z2_max} "
        f"/ Z3 {hr_zones.z2_max}-{hr_zones.z3_max} / Z4 {hr_zones.z3_max}-{hr_zones.z4_max} "
        f"/ Z5>{hr_zones.z4_max} bpm\n"
    )

    # ── 根據有無 Garmin Coach 決定 prompt 策略 ────────────────────────────
    if coach:
        garmin_section = _format_garmin_coach_for_prompt(coach)
        system_prompt = (
            "你是專業馬拉松訓練教練，負責根據跑者的生理數據「微調」Garmin Coach 的訓練建議。\n"
            "你不會取代 Garmin Coach 的計劃結構，只在必要時提出修正。\n\n"
            "**重點原則**：\n"
            "- 你的輸出是對上述 Coach 課表的「修正清單（delta）」，**不要生成新課表**\n"
            "- 只有當生理數據明確顯示問題時才修正，否則「維持 Garmin Coach 建議」\n"
            "- 每個修正都必須引用生理數據並量化（數字、閾值）\n"
            "- 課表關鍵課（長跑/質量課）優先重排到其他日，而非直接刪除\n"
            "- 不要過度保守，跑者要進步就需要適當挑戰\n"
            "- 不要因為「缺乏資料」就降低強度，缺資料時維持原計劃\n\n"
            "**修正時機**（必須有明確證據才修正）：\n"
            "1. HRV 顯著偏低（< 個人基線 -15%） + 睡眠差 → 降強度\n"
            "2. Body Battery < 30 → 改休息\n"
            "3. ACWR > 1.5 → 過度訓練風險，降量\n"
            "4. 體重 / 蛋白質 / 水分異常 → 給營養建議（但不一定要改訓練）\n"
            "不要推測用藥、用藥時間或副作用。\n\n"
            "**長跑補給與腸胃訓練**：只在使用者訊息提供本週補給目標時，"
            "對課表中明確 ≥90 分鐘的長跑附上該目標。"
            "參考近期回報：GI ≥3（腸胃不適）時不要建議加量。"
            "目標或課表缺失時不要推測，也不要排賽日補給。\n\n"
            "**⚠️ 必須引用具體數據！**\n"
            "每個建議都要對應到具體數字。禁止寫「狀態良好」「無需調整」這種空話。\n"
            "範例：「✅ 維持（Readiness 75 HIGH，HRV 52ms GOOD，睡眠分數 82）」\n"
            "      「⚠️ 改 6km LSD（原 LSD 18km）— Readiness 35 LOW，連續 3 天睡眠 < 6h」\n\n"
            "**輸出格式**（用繁體中文 + Markdown，不要用 HTML 標籤）：\n"
            "1. **近期生理摘要**：只列已取得的數據；缺值不可當成零或用推測補齊\n"
            "2. **每日修正表**（一天一行）：\n"
            "   `日期(週X) | Coach 排程(具體) | 維持/修正(具體) | 數據佐證`\n"
            "3. **本週重點 3-5 點**：含營養（蛋白質 XXg/目標 / 水分 XXml/目標）+ 體重趨勢 + 訓練重點\n"
            "4. 如果整週都不需要修正，仍要列出已取得的 Coach 排程；不要補寫缺少的日期"
        )
        user_message = (
            f"{garmin_section}\n\n"
            f"## 跑者檔案\n"
            f"- 體重：{profile.weight_kg}kg（目標 {profile.target_weight_kg}kg）\n"
            f"- VDOT：{profile.vdot}\n"
            f"- 最大心率：{profile.max_hr} bpm\n"
            f"{pace_zone_section}"
            f"- 蛋白質目標：{profile.protein_target_g}g/天\n"
            f"- 飲水目標：{profile.water_target_l}L/天\n"
            f"## 近期生理數據\n{weekly_summary}"
        )
        if race_countdown:
            user_message += f"\n\n{race_countdown}"
        if fuel_target is not None:
            fuel_lo, fuel_hi = fuel_target
            user_message += (
                f"\n\n## 賽中補給（腸胃訓練）\n"
                f"- 本週長跑（≥90 分鐘）補給演練目標：每小時 {fuel_lo}-{fuel_hi}g 碳水\n"
                f"{format_fuel_history(list(context.activity_records))}"
            )
    else:
        # 無 Coach 課表（API + coach_week.json 皆不可用）→ 純身體狀態諮詢模式：
        # 明確聲明未讀到課表、只給身體狀態評估與注意事項，絕不編造課表（T12）
        prompt_template = _load_prompt("training_advisor_prompt.md")
        system_prompt = (
            "你是專業馬拉松訓練教練。目前讀不到 Garmin Coach 課表，"
            "你只做身體狀態諮詢：明確聲明未讀到課表，"
            "僅根據生理數據給身體狀態評估與注意事項，"
            "絕不編造、猜測或輸出任何逐日訓練課表。"
            "用繁體中文 Markdown 輸出，不要 JSON、不要 HTML 標籤。"
        )
        user_message = prompt_template.format(
            weight_kg=profile.weight_kg,
            target_weight_kg=profile.target_weight_kg,
            vdot=profile.vdot,
            max_hr=profile.max_hr,
            easy_pace_min=_format_pace(profile.easy_pace_min),
            easy_pace_max=_format_pace(profile.easy_pace_max),
            weekly_summary=weekly_summary,
        )

    raw_response = _call_ai_api(
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=1536 if coach else 768,
    )

    if not isinstance(raw_response, str) or not raw_response.strip():
        return None

    # Coach 模式：回傳對課表的修正清單（delta）
    if coach:
        return _format_coach_adjustment(raw_response, coach)

    # 無課表模式：包上「未讀到課表」聲明標頭（不依賴 AI 自律）
    return _format_no_coach_advisory(raw_response)


def _format_context_for_prompt(context: WeeklyContext) -> str:
    """將 WeeklyContext 格式化為 prompt 用的文字摘要。"""
    lines = [
        f"- 過去 {context.days} 天",
    ]
    if context.activity_records:
        lines.extend([
            f"- 跑步：{context.run_count} 次，共 {context.total_distance_km} km",
            f"- 重訓：{context.strength_count} 次",
            f"- 訓練負荷：{context.total_training_load}",
        ])
    else:
        lines.append("- 活動紀錄未取得，無法判斷訓練量")
    for label, value, unit in (
        ("平均睡眠分數", context.avg_sleep_score, ""),
        ("平均 HRV", context.avg_hrv, " ms"),
        ("平均安靜心率", context.avg_resting_hr, " bpm"),
        ("慢性訓練負荷", context.chronic_load, ""),
    ):
        lines.append(f"- {label}：{value}{unit}" if value is not None else f"- {label}：無資料")

    # ── 營養 / 體重摘要 ────────────────────────────────────────────────
    has_nutrition = any([
        context.avg_daily_calories,
        context.avg_daily_protein_g,
        context.avg_daily_water_ml,
        context.latest_weight_kg,
    ])
    if has_nutrition:
        lines.append("")
        lines.append("### 營養與體重（依可取得紀錄）：")
        if context.avg_daily_calories is not None:
            lines.append(f"- 平均每日熱量：{context.avg_daily_calories} kcal")
        if context.avg_daily_protein_g is not None:
            lines.append(f"- 平均每日蛋白質：{context.avg_daily_protein_g} g")
        if context.avg_daily_carbs_g is not None:
            lines.append(f"- 平均每日碳水：{context.avg_daily_carbs_g} g")
        if context.avg_daily_fat_g is not None:
            lines.append(f"- 平均每日脂肪：{context.avg_daily_fat_g} g")
        if context.avg_daily_water_ml is not None:
            lines.append(f"- 平均每日飲水：{context.avg_daily_water_ml} ml")
        if context.latest_weight_kg is not None:
            line = f"- 最新體重：{context.latest_weight_kg:.1f} kg"
            if context.weight_trend_kg is not None:
                trend_sign = "+" if context.weight_trend_kg >= 0 else ""
                line += f"（近期變化：{trend_sign}{context.weight_trend_kg:.1f} kg）"
            lines.append(line)
        if context.latest_body_fat_pct is not None:
            lines.append(f"- 最新體脂率：{context.latest_body_fat_pct:.1f}%")

    if context.activity_records:
        lines.append("")
        lines.append("### 最近活動：")
        for act in context.activity_records[:5]:
            line = f"  - {act.start_time or '?'}: {act.activity_type or '?'}"
            if act.distance_km:
                line += f" {act.distance_km}km"
            if act.pace:
                line += f" @ {act.pace}/km"
            if act.training_load:
                line += f" 負荷{act.training_load}"
            lines.append(line)

    return "\n".join(lines)


def _format_no_coach_advisory(ai_response: str) -> str:
    """為「無課表諮詢模式」的 AI 回應加上固定聲明標頭。

    聲明不依賴 AI 自律 — 即使 AI 忘了聲明，標頭也保證使用者知道
    這不是課表（T12）。

    Args:
        ai_response: AI 的身體狀態評估文字。

    Returns:
        含「未讀到 Garmin Coach 課表」聲明的完整訊息。
    """
    header = (
        f"🩺 **身體狀態諮詢** — 未讀到 Garmin Coach 課表\n"
        f"⚠️ 本訊息**不含訓練課表**；本週訓練請以手錶 / Garmin Connect App 上的排程為準\n"
        f"{'─' * 40}\n"
    )
    return header + ai_response


# ── 格式化 ─────────────────────────────────────────────────────────────────


def _format_coach_adjustment(ai_response: str, coach: GarminCoachContext) -> str:
    """將 AI 的修正建議包裝成完整的輸出格式。"""
    lines = [f"🏃 **週訓練修正建議**{_phase_suffix(coach)}", f"📋 {coach.plan_name}"]
    countdown = _race_countdown(_race_days(coach))
    if countdown:
        lines.append(countdown)
    return "\n".join([*lines, "─" * 40, ai_response])


# ── Fallback 計劃 ────────────────────────────────────────────────────────────


def _build_fallback_plan(
    context: WeeklyContext,
    coach: Optional[GarminCoachContext] = None,
) -> str:
    """未產生 AI 建議時，只轉述已取得的課表或身體狀態。"""
    # ── 有 Garmin Coach 時，直接顯示 Coach 排程 ──────────────────────────
    if coach and coach.tasks:
        source = "手動抄錄課表" if coach.current_phase == "MANUAL" else "本週 Garmin Coach 排程"
        lines = [
            f"🏃 **{source}**{_phase_suffix(coach)}",
            f"📋 {coach.plan_name}",
        ]
        countdown = _race_countdown(_race_days(coach))
        if countdown:
            lines.append(countdown)
        lines.extend(["AI 未產生建議，以下為原始課表。", ""])
        for task in coach.tasks:
            d = task.calendar_date
            task_date = _parse_iso_date(d)
            label = f"{d}（{_DAY_NAMES[task_date.weekday()]}）" if task_date else d

            if task.is_rest_day:
                lines.append(f"😴 {label} — 休息")
            elif task.workout_name:
                dist = f" {task.estimated_distance_m / 1000:.1f}km" if task.estimated_distance_m else ""
                hr = f" ({task.workout_description})" if task.workout_description else ""
                lines.append(f"🏃 {label} — {task.workout_name}{dist}{hr}")
            else:
                lines.append(f"📅 {label} — {task.training_effect or '排程中'}")

        target = _fuel_target(coach)
        if target is not None:
            lo, hi = target
            lines.append(
                f"\n⛽ 本週長跑（≥90 分鐘）補給演練目標：每小時 {lo}-{hi}g 碳水"
                "，跑後可回報 /fuel <碳水g> <GI 1-5>"
            )
        return "\n".join(lines)

    lines = [
        "🏃 **本週訓練狀態**",
        "無法取得 Garmin Coach 課表，也沒有可用的手動課表。AI 未產生建議。",
        "本訊息不含訓練課表。可到 Garmin Connect 或手錶查看是否有新排程。",
        "",
        f"**身體狀態摘要（過去 {context.days} 天內可取得的紀錄）**",
    ]
    if context.activity_records:
        lines.append(
            f"- 跑步 {context.run_count} 次，共 {context.total_distance_km} km；"
            f"重訓 {context.strength_count} 次"
        )
    else:
        lines.append("- 活動紀錄未取得，無法判斷跑步或重訓次數")
    metrics = [
        f"- {label}：{value}{unit}"
        for label, value, unit in (
            ("平均睡眠分數", context.avg_sleep_score, ""),
            ("平均 HRV", context.avg_hrv, " ms"),
            ("平均安靜心率", context.avg_resting_hr, " bpm"),
        )
        if value is not None
    ]
    lines.extend(metrics or ["- 睡眠、HRV、安靜心率無可用紀錄"])
    return "\n".join(lines)


# ── Telegram 推播 ────────────────────────────────────────────────────────────


def _send_to_telegram(plan_text: str) -> bool:
    """推送週計劃，只有確定送達才回傳 True。"""
    return send_telegram_text(plan_text)


# ── 工具函式 ─────────────────────────────────────────────────────────────────


def _load_prompt(filename: str) -> str:
    """讀取 prompts 目錄下的 prompt 模板。"""
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def _format_pace(pace_float: float) -> str:
    """將配速浮點數轉為字串（6.5 → '6:30'）。"""
    minutes = int(pace_float)
    seconds = int((pace_float - minutes) * 60)
    return f"{minutes}:{seconds:02d}"


# ── CLI 進入點 ───────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="馬拉松 AI 週訓練建議生成器")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="乾跑模式：只印出計劃，不發送 Telegram",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=28,
        help="歷史數據回溯天數（預設 28）",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        default=False,
        help="推送至 Telegram",
    )
    return parser.parse_args()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    args = _parse_args()

    result = run_weekly_advisor(
        dry_run=args.dry_run,
        days=args.days,
        output_telegram=args.telegram,
    )

    if result is None:
        logger.error("週訓練建議生成失敗")
        sys.exit(1)

    logger.info("週訓練建議完成")
