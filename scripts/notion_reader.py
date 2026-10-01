"""scripts/notion_reader.py — 從 Notion 讀取歷史訓練數據。

提供規則引擎（chronic_load）和 AI 教練（WeeklyContext）所需的歷史上下文。
從 Health DB 和 Activity DB 反向查詢最近 N 天的紀錄。

使用方式::

    from scripts.notion_reader import fetch_health_history, build_weekly_context

    client = get_notion_client(api_key)
    records = fetch_health_history(client, db_id, days=28)
    context = build_weekly_context(records, activities)
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ActivityData, HealthData, NutritionData, WeeklyContext
from utils.logger import get_logger

logger = get_logger(__name__)


# ── 從 Notion 查詢 ──────────────────────────────────────────────────────────


def fetch_health_history(
    client: object,
    database_id: str,
    days: int = 28,
    date_property: str = "Date",
) -> List[HealthData]:
    """查詢 Notion Health DB 最近 N 天的紀錄，轉為 HealthData 清單。

    Args:
        client: Notion Client 物件。
        database_id: Health Database ID。
        days: 回溯天數（預設 28 天）。
        date_property: 日期欄位名稱。

    Returns:
        HealthData 清單，依日期由新到舊排序。
    """
    from notion_client_helper import query_pages

    start_date = (date.today() - timedelta(days=days)).isoformat()
    filter_dict = {
        "property": date_property,
        "date": {"on_or_after": start_date},
    }
    sorts = [{"property": date_property, "direction": "descending"}]

    pages = query_pages(
        client, database_id, filter_dict=filter_dict, sorts=sorts, page_size=100
    )

    results: List[HealthData] = []
    for page in pages:
        props = page.get("properties", {})
        results.append(_parse_health_page(props))

    logger.info("讀取 %d 筆健康紀錄（過去 %d 天）", len(results), days)
    return results


def fetch_activity_history(
    client: object,
    database_id: str,
    days: int = 28,
    date_property: str = "Date",
) -> List[ActivityData]:
    """查詢 Notion Activity DB 最近 N 天的紀錄，轉為 ActivityData 清單。

    Args:
        client: Notion Client 物件。
        database_id: Activity Database ID。
        days: 回溯天數（預設 28 天）。
        date_property: 日期欄位名稱。

    Returns:
        ActivityData 清單，依日期由新到舊排序。
    """
    from notion_client_helper import query_pages

    start_date = (date.today() - timedelta(days=days)).isoformat()
    filter_dict = {
        "property": date_property,
        "date": {"on_or_after": start_date},
    }
    sorts = [{"property": date_property, "direction": "descending"}]

    pages = query_pages(
        client, database_id, filter_dict=filter_dict, sorts=sorts, page_size=100
    )

    results: List[ActivityData] = []
    for page in pages:
        props = page.get("properties", {})
        results.append(_parse_activity_page(props))

    logger.info("讀取 %d 筆活動紀錄（過去 %d 天）", len(results), days)
    return results


def fetch_nutrition_history(
    client: object,
    database_id: str,
    days: int = 28,
    date_property: str = "Date",
) -> List[NutritionData]:
    """查詢 Notion Nutrition DB 最近 N 天的紀錄，轉為 NutritionData 清單。

    Args:
        client: Notion Client 物件。
        database_id: Nutrition Database ID。
        days: 回溯天數（預設 28 天）。
        date_property: 日期欄位名稱。

    Returns:
        NutritionData 清單，依日期由新到舊排序。
    """
    from notion_client_helper import query_pages

    start_date = (date.today() - timedelta(days=days)).isoformat()
    filter_dict = {
        "property": date_property,
        "date": {"on_or_after": start_date},
    }
    sorts = [{"property": date_property, "direction": "descending"}]

    pages = query_pages(
        client, database_id, filter_dict=filter_dict, sorts=sorts, page_size=100
    )

    results: List[NutritionData] = []
    for page in pages:
        props = page.get("properties", {})
        results.append(_parse_nutrition_page(props))

    logger.info("讀取 %d 筆營養紀錄（過去 %d 天）", len(results), days)
    return results


# ── 上下文建構 ───────────────────────────────────────────────────────────────


def build_weekly_context(
    health_records: List[HealthData],
    activity_records: List[ActivityData],
    nutrition_records: Optional[List[NutritionData]] = None,
    days: int = 7,
) -> WeeklyContext:
    """從歷史紀錄建構訓練上下文摘要。

    Args:
        health_records: 健康紀錄清單。
        activity_records: 活動紀錄清單。
        nutrition_records: 營養紀錄清單（可選）。
        days: 摘要涵蓋天數。

    Returns:
        WeeklyContext 匯總。
    """
    running_types = {"running", "trail_running", "treadmill_running", "track_running"}

    total_distance = 0.0
    total_load = 0.0
    run_count = 0
    strength_count = 0

    for act in activity_records:
        act_type = (act.activity_type or "").lower()
        if act_type in running_types:
            run_count += 1
            total_distance += act.distance_km or 0.0
        elif "strength" in act_type:
            strength_count += 1
        total_load += act.training_load or 0.0

    sleep_scores = [
        h.sleep_score for h in health_records if h.sleep_score is not None
    ]
    hrvs = [
        h.hrv_last_night for h in health_records if h.hrv_last_night is not None
    ]
    resting_hrs = [
        h.resting_heart_rate
        for h in health_records
        if h.resting_heart_rate is not None
    ]

    # ── 營養匯總 ─────────────────────────────────────────────────
    nutrition_records = nutrition_records or []

    def _avg(vals: List[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 1) if vals else None

    cals = [n.calories for n in nutrition_records if n.calories]
    proteins = [n.protein_g for n in nutrition_records if n.protein_g]
    carbs = [n.carbs_g for n in nutrition_records if n.carbs_g]
    fats = [n.fat_g for n in nutrition_records if n.fat_g]
    waters = [n.water_ml for n in nutrition_records if n.water_ml]
    weights = [n.weight_kg for n in nutrition_records if n.weight_kg]
    bodyfats = [n.body_fat_pct for n in nutrition_records if n.body_fat_pct]

    # 體重趨勢：最新體重 - 最舊體重（records 已按日期由新到舊排序）
    weight_trend: Optional[float] = None
    if len(weights) >= 2:
        weight_trend = round(weights[0] - weights[-1], 2)

    return WeeklyContext(
        days=days,
        health_records=tuple(health_records),
        activity_records=tuple(activity_records),
        nutrition_records=tuple(nutrition_records),
        total_distance_km=round(total_distance, 1),
        total_training_load=round(total_load, 1),
        run_count=run_count,
        strength_count=strength_count,
        avg_sleep_score=round(sum(sleep_scores) / len(sleep_scores), 1) if sleep_scores else None,
        avg_hrv=round(sum(hrvs) / len(hrvs), 1) if hrvs else None,
        avg_resting_hr=round(sum(resting_hrs) / len(resting_hrs), 1) if resting_hrs else None,
        chronic_load=compute_chronic_load(health_records),
        avg_daily_calories=_avg(cals),
        avg_daily_protein_g=_avg(proteins),
        avg_daily_carbs_g=_avg(carbs),
        avg_daily_fat_g=_avg(fats),
        avg_daily_water_ml=_avg(waters),
        latest_weight_kg=weights[0] if weights else None,
        latest_body_fat_pct=bodyfats[0] if bodyfats else None,
        weight_trend_kg=weight_trend,
    )


def rolling_resting_hr(
    health_records: List[HealthData],
    window: int = 7,
) -> Optional[float]:
    """計算近 N 筆健康紀錄的安靜心率均值（T13：Karvonen 心率區間用）。

    ``health_records`` 假設已依日期由新到舊排序（``fetch_health_history``
    的回傳順序即是如此），因此直接取清單前 ``window`` 筆即為「近 N 天」。

    Args:
        health_records: ``fetch_health_history`` 回傳的健康紀錄清單
            （由新到舊排序）。
        window: 取樣天數（預設近 7 天）。

    Returns:
        安靜心率均值（bpm，四捨五入至小數 1 位）；沒有任何有效紀錄時
        回傳 None，呼叫端應退回 ``utils.vdot_paces.DEFAULT_RESTING_HR``。
    """
    recent = health_records[:window]
    values = [r.resting_heart_rate for r in recent if r.resting_heart_rate is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def compute_chronic_load(
    health_records: List[HealthData],
    min_records: int = 7,
) -> Optional[float]:
    """計算慢性訓練負荷（28 天急性負荷均值）。

    Args:
        health_records: 健康紀錄清單。
        min_records: 最少需要幾筆有效資料才計算。

    Returns:
        慢性負荷均值，資料不足時回傳 None。
    """
    loads = [
        h.acute_load for h in health_records if h.acute_load is not None
    ]
    if len(loads) < min_records:
        logger.warning(
            "慢性負荷計算：僅有 %d 筆有效資料（需 %d 筆），跳過",
            len(loads),
            min_records,
        )
        return None
    return round(sum(loads) / len(loads), 1)


# ── Notion Property 解析 ────────────────────────────────────────────────────


def _get_number(props: Dict, key: str) -> Optional[float]:
    """從 Notion properties 取出 number 值。"""
    prop = props.get(key, {})
    val = prop.get("number")
    return val


def _get_rich_text(props: Dict, key: str) -> Optional[str]:
    """從 Notion properties 取出 rich_text 值。"""
    prop = props.get(key, {})
    texts = prop.get("rich_text", [])
    if texts:
        return texts[0].get("text", {}).get("content")
    return None


def _get_title(props: Dict, key: str) -> Optional[str]:
    """從 Notion properties 取出 title 值。

    title 型別屬性（如 DB 的 ``Name`` 欄位）的 payload 結構與
    rich_text 不同，放在 ``props[key]["title"]`` 陣列。
    """
    prop = props.get(key, {})
    texts = prop.get("title", [])
    if texts:
        return texts[0].get("text", {}).get("content")
    return None


def _get_select(props: Dict, key: str) -> Optional[str]:
    """從 Notion properties 取出 select 值。"""
    prop = props.get(key, {})
    sel = prop.get("select")
    if sel:
        return sel.get("name")
    return None


def _safe_int(val: Optional[float]) -> Optional[int]:
    """安全轉 int。"""
    return int(val) if val is not None else None


def _parse_health_page(props: Dict) -> HealthData:
    """將 Notion Health DB 頁面的 properties 轉為 HealthData。

    Property 名稱必須與 Notion DB 完全一致。
    """
    return HealthData(
        steps=_safe_int(_get_number(props, "Steps")),
        resting_heart_rate=_safe_int(_get_number(props, "Resting HR")),
        sleep_hours=_get_number(props, "Sleep Hours"),
        sleep_score=_safe_int(_get_number(props, "Sleep Score")),
        stress=_safe_int(_get_number(props, "Stress Avg")),
        body_battery=_safe_int(_get_number(props, "Body Battery")),
        calories=_safe_int(_get_number(props, "Active Calories")),
        training_readiness=_safe_int(_get_number(props, "Training Readiness")),
        training_readiness_level=_get_select(props, "Readiness Level"),
        recovery_time=_safe_int(_get_number(props, "Recovery Time")),
        hrv_last_night=_safe_int(_get_number(props, "HRV")),
        hrv_weekly_avg=_safe_int(_get_number(props, "HRV Weekly Avg")),
        hrv_status=_get_select(props, "HRV Status"),
        acute_load=_safe_int(_get_number(props, "Acute Load")),
        fitness_age=_get_number(props, "Fitness Age"),
    )


def _get_date(props: Dict, key: str) -> Optional[str]:
    """從 Notion properties 取出 date 的 start 值（ISO 字串）。"""
    prop = props.get(key, {})
    d = prop.get("date")
    if d:
        return d.get("start")
    return None


def _parse_nutrition_page(props: Dict) -> NutritionData:
    """將 Notion Nutrition DB 頁面的 properties 轉為 NutritionData。

    Property 名稱必須與 create_nutrition_db.js 完全一致。
    """
    return NutritionData(
        date=_get_date(props, "Date"),
        calories=_get_number(props, "Calories"),
        protein_g=_get_number(props, "Protein (g)"),
        carbs_g=_get_number(props, "Carbs (g)"),
        fat_g=_get_number(props, "Fat (g)"),
        water_ml=_get_number(props, "Water (ml)"),
        weight_kg=_get_number(props, "Weight (kg)"),
        body_fat_pct=_get_number(props, "Body Fat (%)"),
    )


def _parse_activity_page(props: Dict) -> ActivityData:
    """將 Notion Activity DB 頁面的 properties 轉為 ActivityData。"""
    return ActivityData(
        activity_id=_safe_int(_get_number(props, "Activity ID")),
        activity_name=_get_title(props, "Name"),
        activity_type=_get_select(props, "Type"),
        start_time=_get_date(props, "Date"),
        distance_km=_get_number(props, "Distance (km)"),
        duration_min=_get_number(props, "Duration (min)"),
        pace=_get_rich_text(props, "Pace"),
        avg_hr=_safe_int(_get_number(props, "Avg HR")),
        max_hr=_safe_int(_get_number(props, "Max HR")),
        calories=_safe_int(_get_number(props, "Calories")),
        avg_cadence=_get_number(props, "Avg Cadence"),
        avg_power=_safe_int(_get_number(props, "Avg Power")),
        elevation_gain=_safe_int(_get_number(props, "Elevation Gain")),
        training_effect_aerobic=_get_number(props, "TE Aerobic"),
        training_effect_anaerobic=_get_number(props, "TE Anaerobic"),
        vo2max=_safe_int(_get_number(props, "VO2 Max")),
        training_load=_get_number(props, "Training Load"),
        rpe=_safe_int(_get_number(props, "RPE")),
        niggle_score=_safe_int(_get_number(props, "Niggle Score")),
        notes=_get_rich_text(props, "Notes"),
        fuel_carbs_g=_get_number(props, "Fuel Carbs (g)"),
        fuel_plan=_get_rich_text(props, "Fuel Plan"),
        gi_score=_safe_int(_get_number(props, "GI Score")),
    )
