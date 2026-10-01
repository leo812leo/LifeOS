"""scripts/health_tracker.py — 每日 Garmin 健康數據追蹤。

功能：

1. 登入 Garmin Connect（支援 OAuth token 快取）
2. 遇到 429 限流時自動退避重試（最多 2 次，等待 60/120 秒）
3. 抓取前一天的：步數、安靜心率、睡眠時數、睡眠分數、平均壓力、身體電量、活動消耗卡路里
4. 防重複寫入：同日已存在記錄則跳過
5. 寫入 Notion Health DB
6. 第二階段（T19）：同一次登入 session 內，順路同步前一天的訓練活動到 Notion
   Activity DB（``sync_activities``）。與健康數據寫入完全失敗隔離 — 活動同步
   出錯只影響 ``health_tracker.activities`` 這條 run_ledger 記錄，不會讓已經
   寫入的健康數據被撤銷或讓主流程中斷。備援路線見 ``health_tracker_web.py``
   （Playwright 網頁版，目前無排程，僅在官方 API 路線失效時手動執行）。

執行方式::

    python scripts/health_tracker.py
"""

import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

# 讓腳本在任何目錄都能正確 import 專案模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

import config
from config import (
    ACTIVITY_DB_ID,
    GARMIN_EMAIL,
    GARMIN_PASSWORD,
    HEALTH_DB_ID,
    NOTION_API_KEY,
    get_activity_api_key,
)
from models import ActivityData
from notion_client_helper import (
    NotionQueryError,
    create_page,
    get_notion_client,
    page_exists_for_date,
    prop_date,
    prop_number,
    prop_rich_text,
    prop_select,
    prop_title,
    query_pages,
)
from utils.logger import get_logger
from utils.run_ledger import append_run

logger = get_logger(__name__)

# Token 快取目錄（garminconnect 0.3.x，與 garmin_login.py 共用）
_GARMIN_TOKEN_DIR = Path(__file__).resolve().parent.parent / ".garmin_tokens"

# 429 重試設定
_MAX_LOGIN_RETRIES = 2        # 帳密登入最大重試次數
_RETRY_BASE_WAIT_SEC = 60.0   # 第一次 429 等待秒數；之後指數成長


# ── Data Transfer Objects ──────────────────────────────────────────────────────


@dataclass
class SleepData:
    """睡眠摘要。

    Attributes:
        sleep_hours: 總睡眠時數（小時）；抓取失敗時為 None。
        sleep_score: 睡眠分數（0–100）；抓取失敗時為 None。
    """

    sleep_hours: Optional[float]
    sleep_score: Optional[int]


@dataclass
class HealthData:
    """每日健康摘要。

    Attributes:
        steps: 總步數。
        resting_heart_rate: 安靜心率（bpm）。
        sleep_hours: 總睡眠時數（小時）。
        sleep_score: 睡眠分數（0–100）。
        stress: 平均壓力值（0–100）。
        body_battery: 身體電量（當日結束時，0–100）。
        calories: 活動消耗卡路里（kcal）。
    """

    steps: Optional[int] = None
    resting_heart_rate: Optional[int] = None
    sleep_hours: Optional[float] = None
    sleep_score: Optional[int] = None
    stress: Optional[int] = None
    body_battery: Optional[int] = None
    calories: Optional[int] = None


# ── 429 退避輔助 ───────────────────────────────────────────────────────────────


def _handle_429_retry(attempt: int, max_retries: int) -> None:
    """處理 429 限流：記錄警告並等待（指數退避）。

    Args:
        attempt: 當前嘗試次數（0-based）。
        max_retries: 最大重試次數。
    """
    if attempt < max_retries - 1:
        wait_seconds = _RETRY_BASE_WAIT_SEC * (2 ** attempt)
        logger.warning(
            "Garmin 429 請求過於頻繁，等待 %.0f 秒後重試（第 %d/%d 次）",
            wait_seconds,
            attempt + 1,
            max_retries,
        )
        time.sleep(wait_seconds)
    else:
        logger.error(
            "Garmin 429，已達最大重試次數（%d），請稍後手動重試。",
            max_retries,
        )


# ── Garmin 登入 ────────────────────────────────────────────────────────────────


def garmin_login() -> Optional[Garmin]:
    """登入 Garmin Connect，支援 OAuth token 快取與 429 自動退避重試。

    優先嘗試載入已儲存的 token，失敗再用帳密登入並儲存新 token。
    帳密登入遇到 429 時，會自動等待並重試最多 ``_MAX_LOGIN_RETRIES`` 次。

    Returns:
        已登入的 :class:`garminconnect.Garmin` 物件；登入失敗時回傳 None。
    """
    _GARMIN_TOKEN_DIR.mkdir(exist_ok=True)
    token_path = str(_GARMIN_TOKEN_DIR)

    def _prompt_mfa() -> str:
        """提示使用者輸入 Garmin MFA 驗證碼。"""
        print("\n[Garmin] 帳號啟用了兩步驟驗證（MFA）。")
        print("[Garmin] 請檢查你的 email 或驗證器，輸入 6 位數驗證碼：")
        return input("驗證碼：").strip()

    for attempt in range(_MAX_LOGIN_RETRIES):
        try:
            client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD, prompt_mfa=_prompt_mfa)
            # tokenstore 參數讓 0.3.x 自動 load/save/refresh token
            client.login(tokenstore=token_path)
            logger.info("Garmin Connect 登入成功（token 快取：%s）", token_path)
            return client

        except GarminConnectAuthenticationError as exc:
            if "MFA" in str(exc):
                logger.error(
                    "Garmin 需要 MFA 驗證但目前非互動模式。"
                    "請先手動執行: python scripts/garmin_login.py"
                )
            else:
                logger.error("Garmin 帳號或密碼錯誤，請確認 .env 設定。")
            return None

        except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as exc:
            if "429" in str(exc):
                _handle_429_retry(attempt, _MAX_LOGIN_RETRIES)
                if attempt >= _MAX_LOGIN_RETRIES - 1:
                    return None
                continue
            logger.error("無法連線到 Garmin Connect：%s", exc)
            return None

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                _handle_429_retry(attempt, _MAX_LOGIN_RETRIES)
                if attempt >= _MAX_LOGIN_RETRIES - 1:
                    return None
            else:
                logger.error("Garmin 登入 HTTP 錯誤 %s：%s", status, exc)
                return None

        except Exception as exc:
            if "429" in str(exc):
                _handle_429_retry(attempt, _MAX_LOGIN_RETRIES)
                if attempt >= _MAX_LOGIN_RETRIES - 1:
                    return None
            else:
                logger.error("Garmin 登入發生未預期錯誤：%s", exc)
                return None

    return None


# ── 抓取健康數據 ───────────────────────────────────────────────────────────────


def fetch_steps(client: Garmin, target_date: str) -> Optional[int]:
    """抓取指定日期的總步數。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串，例如 ``'2024-01-15'``。

    Returns:
        當日總步數；抓取失敗時回傳 None。
    """
    try:
        data = client.get_steps_data(target_date)
        if not data:
            return None
        total = sum(item.get("steps", 0) for item in data)
        logger.info("步數：%d", total)
        return total
    except Exception as exc:
        logger.warning("抓取步數失敗：%s", exc)
        return None


def fetch_resting_heart_rate(client: Garmin, target_date: str) -> Optional[int]:
    """抓取指定日期的安靜心率（bpm）。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串。

    Returns:
        安靜心率（int bpm）；抓取失敗時回傳 None。
    """
    try:
        data = client.get_rhr_day(target_date)
        rhr = (
            data.get("allMetrics", {})
            .get("metricsMap", {})
            .get("WELLNESS_RESTING_HEART_RATE")
        )
        if rhr and len(rhr) > 0:
            value = int(rhr[0].get("value", 0))
            logger.info("安靜心率：%d bpm", value)
            return value
        return None
    except Exception as exc:
        logger.warning("抓取安靜心率失敗：%s", exc)
        return None


def fetch_sleep_data(client: Garmin, target_date: str) -> SleepData:
    """抓取指定日期的睡眠資料。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串。

    Returns:
        :class:`SleepData` 包含睡眠時數與分數；抓取失敗的欄位為 None。
    """
    sleep_hours: Optional[float] = None
    sleep_score: Optional[int] = None

    try:
        data = client.get_sleep_data(target_date)
        daily_sleep = data.get("dailySleepDTO", {})

        sleep_seconds = daily_sleep.get("sleepTimeSeconds")
        if sleep_seconds is not None:
            sleep_hours = round(sleep_seconds / 3600, 2)
            logger.info("睡眠時數：%.2f 小時", sleep_hours)

        score = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value")
        if score is not None:
            sleep_score = int(score)
            logger.info("睡眠分數：%d", sleep_score)

    except Exception as exc:
        logger.warning("抓取睡眠資料失敗：%s", exc)

    return SleepData(sleep_hours=sleep_hours, sleep_score=sleep_score)


def fetch_stress(client: Garmin, target_date: str) -> Optional[int]:
    """抓取指定日期的平均壓力值（0–100）。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串。

    Returns:
        平均壓力值；抓取失敗時回傳 None。
    """
    try:
        data = client.get_stress_data(target_date)
        avg_stress = data.get("avgStressLevel")
        if avg_stress is not None:
            value = int(avg_stress)
            logger.info("平均壓力：%d", value)
            return value
        return None
    except Exception as exc:
        logger.warning("抓取壓力資料失敗：%s", exc)
        return None


def fetch_body_battery(client: Garmin, target_date: str) -> Optional[int]:
    """抓取指定日期結束時的身體電量（0–100）。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串。

    Returns:
        當日結束時的身體電量；抓取失敗時回傳 None。
    """
    try:
        data = client.get_body_battery(target_date)
        if data:
            last_entry = data[-1]
            value = last_entry.get("bodyBattery", {}).get("value")
            if value is not None:
                logger.info("身體電量（結束）：%d", value)
                return int(value)
        return None
    except Exception as exc:
        logger.warning("抓取身體電量失敗：%s", exc)
        return None


def fetch_calories(client: Garmin, target_date: str) -> Optional[int]:
    """抓取指定日期的活動消耗卡路里（kcal）。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串。

    Returns:
        活動消耗卡路里；抓取失敗時回傳 None。
    """
    try:
        data = client.get_stats(target_date)
        calories = data.get("activeKilocalories")
        if calories is not None:
            value = int(calories)
            logger.info("活動消耗：%d kcal", value)
            return value
        return None
    except Exception as exc:
        logger.warning("抓取卡路里失敗：%s", exc)
        return None


def collect_health_data(client: Garmin, target_date: str) -> HealthData:
    """抓取所有健康指標並封裝為 HealthData。

    單一指標抓取失敗不中斷其他指標的抓取（各函式已各自處理 exception）。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串。

    Returns:
        :class:`HealthData` 各欄位抓取失敗時為 None。
    """
    sleep = fetch_sleep_data(client, target_date)
    return HealthData(
        steps=fetch_steps(client, target_date),
        resting_heart_rate=fetch_resting_heart_rate(client, target_date),
        sleep_hours=sleep.sleep_hours,
        sleep_score=sleep.sleep_score,
        stress=fetch_stress(client, target_date),
        body_battery=fetch_body_battery(client, target_date),
        calories=fetch_calories(client, target_date),
    )


# ── 寫入 Notion ────────────────────────────────────────────────────────────────


def write_to_notion(health: HealthData, record_date: str) -> bool:
    """將健康數據寫入 Notion Health DB。

    具備防重複寫入保護：同日已存在記錄時直接回傳 True 跳過。
    只有成功抓到的欄位才寫入，避免 None 造成 Notion API 錯誤。

    Notion DB 欄位（固定，請勿更改）：
        ``Name`` ``Date`` ``Steps`` ``Resting HR`` ``Sleep Hours``
        ``Sleep Score`` ``Stress Avg`` ``Body Battery`` ``Active Calories``

    Args:
        health: 已抓取的健康摘要。
        record_date: 記錄日期，ISO 格式（例如 ``'2024-01-15'``）。

    Returns:
        寫入成功（或已存在跳過）回傳 True，失敗回傳 False。
    """
    client = get_notion_client(NOTION_API_KEY)

    # 防重複寫入：同一天已存在則跳過。
    # 查詢本身失敗時（raise_on_error=True）不可誤判為「今天沒寫過」而繼續寫入 —
    # 那樣會在 Notion 暫時性錯誤時造成重複記錄；改為直接放棄本次寫入並回報失敗。
    try:
        already_exists = page_exists_for_date(
            client, HEALTH_DB_ID, record_date, raise_on_error=True
        )
    except NotionQueryError as exc:
        logger.error(
            "查詢 Notion 既有記錄失敗，為避免重複寫入，本次跳過寫入：%s", exc
        )
        return False

    if already_exists:
        logger.info("健康記錄已存在（%s），跳過寫入。", record_date)
        return True

    properties: Dict[str, object] = {
        "Name": prop_title(f"Health {record_date}"),
        "Date": prop_date(record_date),
    }

    # 欄位名稱（Notion property name） → HealthData 欄位值的對映
    field_map: Dict[str, Optional[float]] = {
        "Steps":           health.steps,
        "Resting HR":      health.resting_heart_rate,
        "Sleep Hours":     health.sleep_hours,
        "Sleep Score":     health.sleep_score,
        "Stress Avg":      health.stress,
        "Body Battery":    health.body_battery,
        "Active Calories": health.calories,
    }

    for field_name, value in field_map.items():
        if value is not None:
            properties[field_name] = prop_number(float(value))

    result = create_page(client, HEALTH_DB_ID, properties)
    return result is not None


# ── 活動同步（T19：官方 API 路線，取代 Playwright 版排程缺口）───────────────────


# Garmin 活動類型：判定為跑步才計算配速（非跑步活動 pace 恆為 None）
_RUNNING_ACTIVITY_TYPES = (
    "running",
    "track_running",
    "trail_running",
    "treadmill_running",
)


def fetch_activities_for_date(client: Garmin, target_date: str) -> List[Dict]:
    """抓取指定日期的活動原始資料（Garmin ``get_activities_by_date``）。

    起訖日設為同一天，讓 Garmin 依 ``startTimeLocal`` 篩選。刻意不在這裡
    捕捉例外 —— 由呼叫端 :func:`sync_activities` 統一攔截，才能正確判斷
    「這次活動同步是否成功」並寫入對應的 run_ledger 記錄。

    Args:
        client: 已登入的 Garmin 物件。
        target_date: ISO 格式日期字串，例如 ``'2024-01-15'``。

    Returns:
        活動原始 dict 清單；Garmin 回傳空/None 時回傳空清單（跑休日的常態）。
    """
    activities = client.get_activities_by_date(target_date, target_date)
    return activities or []


def _parse_pace(duration_sec: Optional[float], distance_m: Optional[float]) -> Optional[str]:
    """計算配速（``分:秒`` / km）。

    Args:
        duration_sec: 持續時間（秒）。
        distance_m: 距離（公尺）。

    Returns:
        配速字串，如 ``'6:51'``；距離或時間缺失/為零時回傳 None（不猜測）。
    """
    if not duration_sec or not distance_m or distance_m <= 0:
        return None
    pace_sec_per_km = duration_sec / (distance_m / 1000)
    minutes = int(pace_sec_per_km // 60)
    seconds = int(pace_sec_per_km % 60)
    return f"{minutes}:{seconds:02d}"


def map_garmin_activity(raw: Dict) -> ActivityData:
    """將 Garmin ``get_activities_by_date`` 回傳的單筆活動 JSON 映射為 ActivityData。

    未知或缺漏的欄位一律回傳 None，不猜測填補值。

    Args:
        raw: 單筆活動的 Garmin API 回應 dict。

    Returns:
        映射後的 :class:`models.ActivityData`。
    """
    distance_m = raw.get("distance")
    duration_sec = raw.get("duration")
    distance_km = round(distance_m / 1000, 2) if distance_m else None
    duration_min = round(duration_sec / 60, 1) if duration_sec else None

    activity_type = (raw.get("activityType") or {}).get("typeKey")
    is_run = activity_type in _RUNNING_ACTIVITY_TYPES
    pace = _parse_pace(duration_sec, distance_m) if is_run else None

    return ActivityData(
        activity_id=raw.get("activityId"),
        activity_name=raw.get("activityName"),
        activity_type=activity_type,
        start_time=raw.get("startTimeLocal"),
        distance_km=distance_km,
        duration_min=duration_min,
        pace=pace,
        avg_hr=raw.get("averageHR"),
        max_hr=raw.get("maxHR"),
        calories=raw.get("calories"),
        avg_cadence=raw.get("averageRunningCadenceInStepsPerMinute"),
        avg_power=raw.get("avgPower"),
        elevation_gain=raw.get("elevationGain"),
        training_effect_aerobic=raw.get("aerobicTrainingEffect"),
        training_effect_anaerobic=raw.get("anaerobicTrainingEffect"),
        vo2max=raw.get("vO2MaxValue"),
        training_load=raw.get("activityTrainingLoad"),
    )


def _activity_exists_in_notion(client: object, activity_id: int) -> bool:
    """檢查 Notion Activity DB 中是否已有該 ``Activity ID`` 的記錄（防重複寫入）。

    ``Activity ID`` 在 Activity DB 是 number 欄位（見
    ``tools/notion-setup/create_activity_db.js``），用數值相等比對。

    Args:
        client: Notion Client 物件。
        activity_id: Garmin 活動 ID。

    Returns:
        已存在回傳 True。
    """
    filter_dict = {
        "property": "Activity ID",
        "number": {"equals": activity_id},
    }
    pages = query_pages(client, ACTIVITY_DB_ID, filter_dict=filter_dict, page_size=1)
    return len(pages) > 0


def write_activity_to_notion(activity: ActivityData) -> Optional[bool]:
    """將單筆活動寫入 Notion Activity DB（含防重複）。

    Notion Activity DB 欄位（固定，請勿更改；見
    ``tools/notion-setup/create_activity_db.js``）：
        ``Name`` ``Date`` ``Type`` ``Distance (km)`` ``Duration (min)``
        ``Pace`` ``Avg HR`` ``Max HR`` ``Calories`` ``Avg Cadence``
        ``Avg Power`` ``Elevation Gain`` ``TE Aerobic`` ``TE Anaerobic``
        ``VO2 Max`` ``Training Load`` ``Activity ID`` ``Notes``

    Args:
        activity: 已映射的活動資料。

    Returns:
        三態結果，讓呼叫端能區分「新寫入」與「跳過」（用於 run_ledger 的
        ``wrote_notion`` 語意正確性）：

        - ``True``：新寫入一筆頁面。
        - ``None``：該 ``activity_id`` 已存在，跳過（非失敗）。
        - ``False``：缺少 ``activity_id``（無法防重複比對）或 Notion API 失敗。
    """
    if activity.activity_id is None:
        logger.warning(
            "活動缺少 activity_id，無法防重複比對，略過寫入：%s",
            activity.activity_name,
        )
        return False

    # Activity DB 在獨立 workspace，須用專屬 key（get_activity_api_key，2026-07 事故教訓）
    client = get_notion_client(get_activity_api_key())

    if _activity_exists_in_notion(client, activity.activity_id):
        logger.info("活動已存在（Activity ID=%s），跳過寫入。", activity.activity_id)
        return None

    act_date = activity.start_time[:10] if activity.start_time else None

    properties: Dict[str, object] = {
        "Name": prop_title(activity.activity_name or "Activity"),
        "Activity ID": prop_number(float(activity.activity_id)),
    }
    if act_date:
        properties["Date"] = prop_date(act_date)
    if activity.activity_type:
        properties["Type"] = prop_select(activity.activity_type)
    if activity.pace:
        properties["Pace"] = prop_rich_text(activity.pace)

    number_fields: Dict[str, Optional[float]] = {
        "Distance (km)":   activity.distance_km,
        "Duration (min)":  activity.duration_min,
        "Avg HR":          float(activity.avg_hr) if activity.avg_hr is not None else None,
        "Max HR":          float(activity.max_hr) if activity.max_hr is not None else None,
        "Calories":        float(activity.calories) if activity.calories is not None else None,
        "Avg Cadence":     activity.avg_cadence,
        "Avg Power":       float(activity.avg_power) if activity.avg_power is not None else None,
        "Elevation Gain":  float(activity.elevation_gain) if activity.elevation_gain is not None else None,
        "TE Aerobic":      activity.training_effect_aerobic,
        "TE Anaerobic":    activity.training_effect_anaerobic,
        "VO2 Max":         float(activity.vo2max) if activity.vo2max is not None else None,
        "Training Load":   activity.training_load,
    }
    for field_name, value in number_fields.items():
        if value is not None:
            properties[field_name] = prop_number(float(value))

    result = create_page(client, ACTIVITY_DB_ID, properties)
    return True if result is not None else False


def sync_activities(client: Garmin, target_date: str) -> None:
    """第二階段：把指定日期的訓練活動同步到 Notion Activity DB。

    與健康數據寫入完全失敗隔離：任何例外都在此攔截，只影響
    ``health_tracker.activities`` 這條 run_ledger 記錄，絕不往外拋出、也不會
    讓已經寫入的健康數據紀錄被撤銷。當天沒有活動（跑休日）是常態，只記一行
    log，不告警、不計為失敗。

    Args:
        client: 已登入的 Garmin 物件（與健康數據抓取共用同一個 session）。
        target_date: ISO 格式日期字串，例如 ``'2024-01-15'``。
    """
    if not ACTIVITY_DB_ID or ACTIVITY_DB_ID == "placeholder":
        logger.warning("ACTIVITY_DB_ID 尚未設定，跳過活動同步。")
        append_run("health_tracker.activities", ok=True, wrote_notion=False)
        return

    wrote_any = False
    all_ok = True

    # 整段（抓取 + 逐筆處理）都包在同一個 try 內：任何未預期的錯誤（包含抓取
    # 階段本身、或回傳格式不如預期導致的例外）都要在這裡攔截並記錄失敗的
    # run_ledger，絕不讓例外往外傳到 main() 影響健康數據寫入。
    try:
        raw_activities = fetch_activities_for_date(client, target_date)

        if not raw_activities:
            logger.info(
                "目標日期 %s 無活動紀錄（跑休日，或手錶尚未同步）。", target_date
            )
            append_run("health_tracker.activities", ok=True, wrote_notion=False)
            return

        for raw in raw_activities:
            try:
                activity = map_garmin_activity(raw)
                outcome = write_activity_to_notion(activity)
                if outcome is True:
                    wrote_any = True
                    logger.info(
                        "已同步活動：%s（%s，%s km）",
                        activity.activity_name,
                        activity.activity_type,
                        activity.distance_km,
                    )
                elif outcome is None:
                    logger.debug(
                        "活動已存在，略過：activity_id=%s", activity.activity_id
                    )
                else:
                    all_ok = False
                    logger.error("寫入活動失敗（activity_id=%s）", activity.activity_id)
            except Exception as exc:
                all_ok = False
                logger.error(
                    "處理單筆活動時發生未預期錯誤（略過，不影響健康數據）：%s", exc
                )

    except Exception as exc:
        logger.error("活動同步失敗（不影響健康數據寫入）：%s", exc)
        append_run("health_tracker.activities", ok=False, wrote_notion=wrote_any)
        return

    append_run("health_tracker.activities", ok=all_ok, wrote_notion=wrote_any)


# ── 失敗告警 ───────────────────────────────────────────────────────────────────


def _alert_failure(message: str) -> None:
    """推送健康追蹤失敗告警到 Telegram。

    Telegram 未設定或推送本身失敗都不會拋出例外，避免干擾主流程。

    Args:
        message: 告警內容（不可包含金鑰、token 等敏感資訊）。
    """
    try:
        from scripts.telegram_bot import safe_send_alert

        safe_send_alert("health_tracker.py", message)
    except Exception as exc:
        logger.warning("Telegram 警告推送失敗（忽略）：%s", exc)


# ── 主程式 ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """健康追蹤主程式。"""
    logger.info("=" * 60)
    logger.info("健康追蹤腳本啟動")
    logger.info("=" * 60)

    config.validate_config()

    # 前一天的日期（Garmin 資料通常在隔天才完整同步）
    target_date = (date.today() - timedelta(days=1)).isoformat()
    logger.info("抓取日期：%s", target_date)

    garmin = garmin_login()
    if garmin is None:
        logger.error("Garmin 登入失敗，腳本終止。")
        _alert_failure(
            "Garmin 登入失敗。請執行 `python scripts/garmin_login.py` 重新登入（可能需要 MFA）。"
        )
        append_run("health_tracker.py", ok=False, wrote_notion=False)
        sys.exit(1)

    health = collect_health_data(garmin, target_date)

    logger.info("─ 步數：%s",        f"{health.steps:,}" if health.steps else "N/A")
    logger.info("─ 安靜心率：%s",    f"{health.resting_heart_rate} bpm" if health.resting_heart_rate else "N/A")
    logger.info("─ 睡眠時數：%s",    f"{health.sleep_hours} 小時" if health.sleep_hours else "N/A")
    logger.info("─ 睡眠分數：%s",    str(health.sleep_score) if health.sleep_score else "N/A")
    logger.info("─ 平均壓力：%s",    str(health.stress) if health.stress else "N/A")
    logger.info("─ 身體電量：%s",    str(health.body_battery) if health.body_battery else "N/A")
    logger.info("─ 活動消耗：%s",    f"{health.calories} kcal" if health.calories else "N/A")

    # 關鍵欄位（步數、睡眠、安靜心率）全為 None → 手錶尚未同步，寫入近空紀錄
    # 只會讓 dedup 永久擋住當日補值。不寫、告警，讓使用者稍後手動重跑。
    key_fields_missing = (
        health.steps is None
        and health.sleep_hours is None
        and health.resting_heart_rate is None
    )
    if key_fields_missing:
        logger.error(
            "關鍵欄位（步數/睡眠/安靜心率）全為 None，Garmin 尚未同步，跳過寫入。"
        )
        _alert_failure(f"Garmin 尚未同步，今日稍後手動重跑（日期：{target_date}）。")
        append_run("health_tracker.py", ok=False, wrote_notion=False)
        sys.exit(1)

    if HEALTH_DB_ID and HEALTH_DB_ID != "placeholder":
        success = write_to_notion(health, target_date)
        if success:
            logger.info("✓ 已成功寫入 Notion Health DB")
            append_run("health_tracker.py", ok=True, wrote_notion=True)
        else:
            logger.error("✗ 寫入 Notion 失敗")
            _alert_failure(f"Notion Health DB 寫入失敗（日期：{target_date}）。")
            append_run("health_tracker.py", ok=False, wrote_notion=False)
    else:
        logger.warning("HEALTH_DB_ID 尚未設定，跳過 Notion 寫入。")
        append_run("health_tracker.py", ok=True, wrote_notion=False)

    # 第二階段（T19）：同一次登入 session 順路同步活動，失敗完全隔離於健康數據之外
    sync_activities(garmin, target_date)

    logger.info("健康追蹤腳本完成")


if __name__ == "__main__":
    main()
