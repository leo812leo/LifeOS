"""scripts/weekly_review.py — 每週五 18:00 自動週回顧。

讀取本週數據，AI 分析每個 KR 的進展狀態，給出策略檢視建議。

流程：
1. 讀本週 Tasks / Activity / Health / Nutrition / Investment 摘要
2. 讀所有 Active KR（從 OKR DB）
3. AI 分析：哪些 KR On Track / At Risk / Off Track + 調整建議
4. 純程式計算本週「建議遵循率」（不經 AI，見 T15；``utils/adherence.py``）
5. 寫入 Weekly Review DB（如果有）+ 推 Telegram

T15 備註：Weekly Review DB 目前不存在（.env 無對應 DB ID），本腳本從未真正
寫入過 Notion——docstring 過去說「如果有」是願望不是現況。遵循率一律只印
在 Telegram 訊息裡，不嘗試寫 DB。

使用方式::

    python scripts/weekly_review.py            # 推 Telegram
    python scripts/weekly_review.py --dry-run  # 只印不推
"""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from notion_client_helper import get_notion_client, query_pages
from utils.adherence import (
    compute_adherence,
    format_adherence_line,
    read_advice_log,
)
from utils.logger import get_logger
from utils.run_ledger import append_run
from utils.sleep import sleep_debt_7d

logger = get_logger(__name__)


# ── KR 資料結構 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyResult:
    """單個 Key Result。"""
    name: str
    target: Optional[float]
    current: Optional[float]
    progress_pct: Optional[float]
    status: Optional[str]
    unit: Optional[str]
    strategy_notes: Optional[str]
    last_reviewed: Optional[str]


# ── 主流程 ──────────────────────────────────────────────────────────────


def run_weekly_review(dry_run: bool = False) -> Optional[str]:
    """執行週回顧。"""
    logger.info("=" * 60)
    logger.info("週回顧 — %s", date.today().isoformat())
    logger.info("=" * 60)

    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # 本週一
    week_end = week_start + timedelta(days=6)             # 本週日

    # ── 讀取 KR ─────────────────────────────────────────────────────────
    krs = _fetch_active_krs()
    if not krs:
        logger.warning("找不到 Active KR — 跳過 KR 分析")

    # ── 讀取本週數據摘要 ─────────────────────────────────────────────────
    summary = _build_week_summary(week_start, week_end)

    # ── AI 分析 ──────────────────────────────────────────────────────────
    review = _generate_weekly_review(week_start, week_end, summary, krs)

    if not review:
        logger.error("週回顧生成失敗")
        append_run("weekly_review.py", ok=False, wrote_notion=False)
        return None

    # ── 建議遵循率（T15，純程式計算，不經 AI）───────────────────────────
    adherence_line = _compute_week_adherence(week_start, week_end)
    if adherence_line:
        review = f"{review}\n\n{adherence_line}"

    # ── 睡眠趨勢（T18，純程式計算，不經 AI）─────────────────────────────
    sleep_line = _compute_week_sleep_trend(week_start, week_end)
    if sleep_line:
        review = f"{review}\n\n{sleep_line}"

    # ── 輸出 ────────────────────────────────────────────────────────────
    if dry_run:
        logger.info("乾跑模式 — 內容如下：")
        sys.stdout.buffer.write(("\n" + review + "\n").encode("utf-8"))
    else:
        try:
            from scripts.telegram_bot import send_text
            ok = send_text(review)
            if ok:
                logger.info("✅ 已推送至 Telegram")
            else:
                logger.warning("Telegram 未設定或推送失敗")
        except Exception as exc:
            logger.error("推送失敗：%s", exc)

    # weekly_review 本身不寫 Notion DB（只讀歷史資料、推 Telegram 文字），
    # wrote_notion 恆為 False。
    append_run("weekly_review.py", ok=True, wrote_notion=False)

    return review


# ── 讀 KR ──────────────────────────────────────────────────────────────


def _fetch_active_krs() -> List[KeyResult]:
    """從 OKR DB 讀取所有 Active 的 Key Results。"""
    api_key = (
        os.getenv("OKR_NOTION_API_KEY")
        or os.getenv("NUTRITION_NOTION_API_KEY")
        or os.getenv("NOTION_API_KEY", "")
    )
    db_id = os.getenv("KR_DB_ID", "")
    if not db_id:
        logger.warning("KR_DB_ID 未設定")
        return []

    try:
        client = get_notion_client(api_key)
        # 排除 Done / Paused 的 KR
        filter_dict = {
            "or": [
                {"property": "Status", "select": {"equals": "On Track"}},
                {"property": "Status", "select": {"equals": "At Risk"}},
                {"property": "Status", "select": {"equals": "Off Track"}},
            ]
        }
        pages = query_pages(client, db_id, filter_dict=filter_dict, page_size=50)
    except Exception as exc:
        logger.warning("KR 讀取失敗：%s", exc)
        return []

    krs: List[KeyResult] = []
    for page in pages:
        props = page.get("properties", {})
        krs.append(KeyResult(
            name=_get_title(props, "Name"),
            target=_get_num(props, "Target Value"),
            current=_get_num(props, "Current Value"),
            progress_pct=_get_formula_num(props, "Progress %"),
            status=_get_select(props, "Status"),
            unit=_get_text(props, "Unit"),
            strategy_notes=_get_text(props, "Strategy Notes"),
            last_reviewed=_get_date(props, "Last Reviewed"),
        ))

    logger.info("讀取 %d 個 Active KR", len(krs))
    return krs


# ── 本週數據摘要 ──────────────────────────────────────────────────────


def _build_week_summary(week_start: date, week_end: date) -> Dict:
    """讀取本週各種數據摘要。"""
    from scripts.notion_reader import (
        fetch_activity_history,
        fetch_health_history,
        fetch_nutrition_history,
    )

    notion_api_key = os.getenv("NOTION_API_KEY", "")
    nutrition_api_key = os.getenv("NUTRITION_NOTION_API_KEY", notion_api_key)
    activity_api_key = os.getenv("ACTIVITY_NOTION_API_KEY", notion_api_key)

    health_db = os.getenv("HEALTH_DB_ID", "")
    activity_db = os.getenv("ACTIVITY_DB_ID", "")
    nutrition_db = os.getenv("NUTRITION_DB_ID", "")

    summary: Dict = {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
    }

    days_back = (date.today() - week_start).days + 1

    # Health
    if health_db:
        try:
            client = get_notion_client(notion_api_key)
            health_records = fetch_health_history(client, health_db, days=days_back)
            if health_records:
                sleep_scores = [h.sleep_score for h in health_records if h.sleep_score]
                hrvs = [h.hrv_last_night for h in health_records if h.hrv_last_night]
                rhrs = [h.resting_heart_rate for h in health_records if h.resting_heart_rate]
                summary["health"] = {
                    "days_count": len(health_records),
                    "avg_sleep_score": round(sum(sleep_scores) / len(sleep_scores), 1) if sleep_scores else None,
                    "avg_hrv_ms": round(sum(hrvs) / len(hrvs), 1) if hrvs else None,
                    "avg_rhr_bpm": round(sum(rhrs) / len(rhrs), 1) if rhrs else None,
                }
        except Exception as exc:
            logger.warning("Health 讀取失敗：%s", exc)

    # Activity
    if activity_db:
        try:
            client = get_notion_client(activity_api_key)
            acts = fetch_activity_history(client, activity_db, days=days_back)
            if acts:
                running = [a for a in acts if (a.activity_type or "").lower() in ("running", "trail_running")]
                strength = [a for a in acts if "strength" in (a.activity_type or "").lower()]
                summary["activity"] = {
                    "total_count": len(acts),
                    "run_count": len(running),
                    "total_run_km": round(sum(a.distance_km or 0 for a in running), 1),
                    "total_load": round(sum(a.training_load or 0 for a in acts), 1),
                    "strength_count": len(strength),
                }
        except Exception as exc:
            logger.warning("Activity 讀取失敗：%s", exc)

    # Nutrition
    if nutrition_db:
        try:
            client = get_notion_client(nutrition_api_key)
            nutrs = fetch_nutrition_history(client, nutrition_db, days=days_back)
            if nutrs:
                cals = [n.calories for n in nutrs if n.calories]
                proteins = [n.protein_g for n in nutrs if n.protein_g]
                waters = [n.water_ml for n in nutrs if n.water_ml]
                weights = [n.weight_kg for n in nutrs if n.weight_kg]
                summary["nutrition"] = {
                    "days_with_data": len(nutrs),
                    "avg_calories": round(sum(cals) / len(cals)) if cals else None,
                    "avg_protein_g": round(sum(proteins) / len(proteins), 1) if proteins else None,
                    "avg_water_ml": round(sum(waters) / len(waters)) if waters else None,
                    "weight_change_kg": round(weights[0] - weights[-1], 2) if len(weights) >= 2 else None,
                    "latest_weight_kg": round(weights[0], 1) if weights else None,
                }
        except Exception as exc:
            logger.warning("Nutrition 讀取失敗：%s", exc)

    return summary


# ── 建議遵循率（T15）─────────────────────────────────────────────────────


def _compute_week_adherence(week_start: date, week_end: date) -> Optional[str]:
    """計算本週「建議遵循率」，回傳一行供附加到 Telegram 訊息的文字。

    純程式計算（不經 AI）：讀 ``logs/advice_log.jsonl`` 的建議判定，比對
    Activity DB 當天實際訓練強度（見 ``utils/adherence.py``）。任何一步查無
    資料都安全降級為 None（呼叫端跳過此行），不影響週回顧主流程。

    Args:
        week_start: 週起始日（含，本週一）。
        week_end: 週結束日（含，本週日）。

    Returns:
        一行遵循率文字；本週無建議紀錄、或全部天數都無 Activity 資料可比對
        （因此無法判定）時回傳 None。
    """
    try:
        advice_entries = read_advice_log(week_start, week_end)
        if not advice_entries:
            logger.info("本週無 advice_log 紀錄，跳過遵循率計算")
            return None

        activities_by_date = _fetch_week_activities_by_date(week_start, week_end)
        result = compute_adherence(advice_entries, activities_by_date)
        return format_adherence_line(result)
    except Exception as exc:
        logger.warning("遵循率計算失敗（忽略，不影響週回顧）：%s", exc)
        return None


def _fetch_week_activities_by_date(
    week_start: date, week_end: date
) -> Dict[str, List]:
    """讀取本週 Activity DB 紀錄，依日期分組（供遵循率比對用）。

    Activity DB 由 ``health_tracker_web.py`` 手動觸發寫入、目前無排程，
    未設定 ``ACTIVITY_DB_ID`` 或查詢失敗都回傳空 dict（上層會把該週所有天
    都判定為「無法判定」，見 ``utils.adherence.compute_adherence``）。

    Args:
        week_start: 週起始日（含）。
        week_end: 週結束日（含）。

    Returns:
        日期字串（YYYY-MM-DD）→ 當天 ActivityData 清單。
    """
    from scripts.notion_reader import fetch_activity_history

    activity_db = os.getenv("ACTIVITY_DB_ID", "")
    if not activity_db:
        return {}

    notion_api_key = os.getenv("NOTION_API_KEY", "")
    activity_api_key = os.getenv("ACTIVITY_NOTION_API_KEY", notion_api_key)
    days_back = (date.today() - week_start).days + 1

    try:
        client = get_notion_client(activity_api_key)
        acts = fetch_activity_history(client, activity_db, days=days_back)
    except Exception as exc:
        logger.warning("Activity 讀取失敗（遵循率計算）：%s", exc)
        return {}

    by_date: Dict[str, List] = {}
    for act in acts:
        day = (act.start_time or "")[:10]
        if not day or not (week_start.isoformat() <= day <= week_end.isoformat()):
            continue
        by_date.setdefault(day, []).append(act)
    return by_date


# ── 睡眠趨勢（T18）───────────────────────────────────────────────────────


def _fetch_health_records_for_sleep_trend(days: int = 14) -> List:
    """讀取近 N 天（預設 14）Health DB 紀錄，供本週/上週睡眠比較用。

    比照 ``rolling_resting_hr``/``compute_chronic_load`` 的既有慣例，用位置
    切片（前 7 筆＝本週、接下來 7 筆＝上週）而非依日曆週對齊 —— Health DB
    紀錄本身沒有可靠的曆法週邊界可用，位置切片是本專案既有的一致做法。

    Args:
        days: 回溯天數（預設 14，供本週 + 上週各 7 天比較）。

    Returns:
        HealthData 清單（依日期新到舊）；``HEALTH_DB_ID`` 未設定或查詢失敗
        時回傳空清單。
    """
    from scripts.notion_reader import fetch_health_history

    health_db = os.getenv("HEALTH_DB_ID", "")
    if not health_db:
        return []

    notion_api_key = os.getenv("NOTION_API_KEY", "")
    try:
        client = get_notion_client(notion_api_key)
        return fetch_health_history(client, health_db, days=days)
    except Exception as exc:
        logger.warning("Health 讀取失敗（睡眠趨勢計算）：%s", exc)
        return []


def _compute_week_sleep_trend(week_start: date, week_end: date) -> Optional[str]:
    """計算本週睡眠均值/債務，與上週比較出趨勢，回傳一行文字（T18）。

    純程式計算（不經 AI）：本週／上週各取 7 筆最近的 Health DB 紀錄
    （見 ``_fetch_health_records_for_sleep_trend``），比較兩週平均睡眠時數
    決定趨勢箭頭（僅供參考，非統計顯著性判定）。任何一步查無資料都安全
    降級為 None，不影響週回顧主流程。

    Args:
        week_start: 週起始日（含，本週一，目前僅用於介面一致性，實際取樣
            不依曆法週對齊，見 ``_fetch_health_records_for_sleep_trend``）。
        week_end: 週結束日（含，本週日）。

    Returns:
        一行睡眠趨勢文字；本週無任何睡眠資料時回傳 None。
    """
    try:
        records = _fetch_health_records_for_sleep_trend()
        if not records:
            logger.info("本週無 Health 紀錄，跳過睡眠趨勢計算")
            return None

        this_week = records[:7]
        last_week = records[7:14]

        this_valid = [h.sleep_hours for h in this_week if h.sleep_hours is not None]
        if not this_valid:
            return None
        this_avg = round(sum(this_valid) / len(this_valid), 1)
        debt = sleep_debt_7d(this_week)
        debt_str = f"{debt:.1f}h" if debt is not None else "?"

        last_valid = [h.sleep_hours for h in last_week if h.sleep_hours is not None]
        trend_part = ""
        if last_valid:
            last_avg = sum(last_valid) / len(last_valid)
            if this_avg > last_avg + 0.1:
                arrow = "↑"
            elif this_avg < last_avg - 0.1:
                arrow = "↓"
            else:
                arrow = "→"
            trend_part = f"，趨勢 {arrow}"

        return f"😴 睡眠：7 日均 {this_avg}h（債務 {debt_str}{trend_part}）"
    except Exception as exc:
        logger.warning("睡眠趨勢計算失敗（忽略，不影響週回顧）：%s", exc)
        return None


# ── AI 週回顧生成 ──────────────────────────────────────────────────────


def _generate_weekly_review(
    week_start: date,
    week_end: date,
    summary: Dict,
    krs: List[KeyResult],
) -> Optional[str]:
    """呼叫 AI 生成週回顧文字。"""
    from scripts.ai_coach import _call_ai_api

    # ── 組 prompt ──────────────────────────────────────────────────────
    week_str = f"{week_start.isoformat()} ~ {week_end.isoformat()}"

    summary_lines = [f"## 本週數據摘要（{week_str}）"]
    if "health" in summary:
        h = summary["health"]
        summary_lines.append(
            f"- **健康**（{h.get('days_count', 0)} 天有資料）："
            f"睡眠分數均 {h.get('avg_sleep_score', '?')} / "
            f"HRV 均 {h.get('avg_hrv_ms', '?')}ms / "
            f"安靜心率均 {h.get('avg_rhr_bpm', '?')}bpm"
        )
    else:
        summary_lines.append("- **健康**：無資料")

    if "activity" in summary:
        a = summary["activity"]
        summary_lines.append(
            f"- **訓練**：跑步 {a.get('run_count', 0)} 次共 {a.get('total_run_km', 0)}km / "
            f"重訓 {a.get('strength_count', 0)} 次 / "
            f"訓練負荷總計 {a.get('total_load', 0)}"
        )
    else:
        summary_lines.append("- **訓練**：無資料")

    if "nutrition" in summary:
        n = summary["nutrition"]
        weight_change = n.get("weight_change_kg")
        weight_trend_str = ""
        if weight_change is not None:
            sign = "+" if weight_change >= 0 else ""
            weight_trend_str = f"（變化 {sign}{weight_change}kg）"
        summary_lines.append(
            f"- **營養**（{n.get('days_with_data', 0)} 天有紀錄）："
            f"熱量均 {n.get('avg_calories', '?')}kcal / "
            f"蛋白質均 {n.get('avg_protein_g', '?')}g / "
            f"水分均 {n.get('avg_water_ml', '?')}ml / "
            f"體重 {n.get('latest_weight_kg', '?')}kg {weight_trend_str}"
        )
    else:
        summary_lines.append("- **營養**：無資料")

    summary_section = "\n".join(summary_lines)

    # KR 區塊
    kr_section = "## Active Key Results"
    if krs:
        kr_lines = []
        for kr in krs:
            progress = f"{kr.progress_pct:.0f}%" if kr.progress_pct is not None else "?%"
            current = kr.current if kr.current is not None else "?"
            target = kr.target if kr.target is not None else "?"
            unit = kr.unit or ""
            last_rev = f"（上次檢視 {kr.last_reviewed}）" if kr.last_reviewed else ""
            kr_lines.append(
                f"- **{kr.name}**：{current}/{target} {unit} → {progress} | "
                f"狀態 {kr.status or '?'} {last_rev}"
            )
            if kr.strategy_notes:
                kr_lines.append(f"  策略：{kr.strategy_notes}")
        kr_section += "\n" + "\n".join(kr_lines)
    else:
        kr_section += "\n（OKR DB 尚未填入 KR，本週純看數據）"

    system_prompt = (
        "你是個人成長教練 + 馬拉松訓練教練。每週五，根據跑者本週數據和 OKR 進展，"
        "提供結構化週回顧。\n\n"
        "**核心原則：**\n"
        "1. 每個結論必須引用具體數字（禁止「狀態良好」「進展順利」這種空話）\n"
        "2. 對每個 KR 給「策略檢視結論」：On Track / At Risk / Off Track\n"
        "3. 對 At Risk / Off Track 的 KR 提出具體調整建議（換策略、改目標值、暫停）\n"
        "4. 連續 2 週無進展 → 建議檢視策略；連續 4 週無進展 → 強制調整 KR\n\n"
        "**輸出格式（繁體中文 + Markdown，用 ** 雙星號 標粗體）：**\n\n"
        "**📅 本週回顧（{date_range}）**\n"
        "**━━━━━━━━━━━━**\n\n"
        "**🏃 訓練表現**：3-4 句具體評估，引用本週實際數據對比目標\n\n"
        "**❤️ 身體狀態**：HRV / 睡眠 / 安靜心率趨勢分析\n\n"
        "**🥗 營養與體重**：熱量赤字評估、蛋白質達標率、體重變化分析\n\n"
        "**🎯 Key Results 檢視**（每個 KR 一段）：\n"
        "對每個 Active KR 給「狀態判斷 + 數據依據 + 行動建議」\n\n"
        "**💡 下週重點 3-5 點**：具體可執行的優先項\n\n"
        "全部不超過 600 字，言之有物。"
    )

    user_message = f"{summary_section}\n\n{kr_section}"

    response = _call_ai_api(
        system_prompt=system_prompt.replace("{date_range}", week_str),
        user_message=user_message,
        max_tokens=2048,
    )

    if response is None:
        return None

    return response


# ── Notion property 解析 ──────────────────────────────────────────────


def _get_title(props: Dict, key: str) -> str:
    prop = props.get(key, {})
    titles = prop.get("title", [])
    if titles:
        return titles[0].get("text", {}).get("content", "")
    return ""


def _get_text(props: Dict, key: str) -> Optional[str]:
    prop = props.get(key, {})
    texts = prop.get("rich_text", [])
    if texts:
        return texts[0].get("text", {}).get("content")
    return None


def _get_num(props: Dict, key: str) -> Optional[float]:
    prop = props.get(key, {})
    return prop.get("number")


def _get_formula_num(props: Dict, key: str) -> Optional[float]:
    prop = props.get(key, {})
    formula = prop.get("formula", {})
    return formula.get("number")


def _get_select(props: Dict, key: str) -> Optional[str]:
    prop = props.get(key, {})
    sel = prop.get("select")
    if sel:
        return sel.get("name")
    return None


def _get_date(props: Dict, key: str) -> Optional[str]:
    prop = props.get(key, {})
    d = prop.get("date")
    if d:
        return d.get("start")
    return None


# ── CLI 進入點 ─────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="每週回顧 + KR 策略檢視")
    parser.add_argument("--dry-run", action="store_true", help="只印出，不推送")
    args = parser.parse_args()

    result = run_weekly_review(dry_run=args.dry_run)
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
