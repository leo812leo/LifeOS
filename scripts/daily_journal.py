"""scripts/daily_journal.py — 每日自動建立 Notion 日記頁面。

功能：

1. 在 Notion Daily Journal DB 建立當日頁面（防重複寫入）
2. 自動從 Notion Health DB 讀取昨天的健康摘要，填入日記頁面
3. 若 Journal DB 有「Health Summary」欄位，自動帶入健康數字；
   若無此欄位，退回只建立 Name + Date

前置條件：
- 在 .env 填入 JOURNAL_DB_ID
- Notion Journal DB 至少需有 Name（title）和 Date 兩個欄位
- 可選：加入 Rich Text 欄位 "Health Summary" 以自動帶入健康摘要

執行方式::

    python scripts/daily_journal.py
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 讓腳本在任何目錄都能正確 import 專案模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from config import HEALTH_DB_ID, JOURNAL_DB_ID, NOTION_API_KEY
from notion_client_helper import (
    create_page,
    get_notion_client,
    page_exists_for_date,
    prop_date,
    prop_rich_text,
    prop_title,
    query_pages,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ── 健康摘要讀取 ───────────────────────────────────────────────────────────────


def fetch_yesterday_health_summary(yesterday_date: str) -> Optional[str]:
    """從 Notion Health DB 讀取昨天的健康數據，組成摘要字串。

    Args:
        yesterday_date: 昨天的日期，ISO 格式（例如 ``'2024-01-15'``）。

    Returns:
        健康摘要字串（例如 ``'昨日健康：步數 8,000 | 睡眠 7.5h | ...'``）；
        查無資料時回傳 None。
    """
    if not HEALTH_DB_ID or HEALTH_DB_ID == "placeholder":
        return None

    client = get_notion_client(NOTION_API_KEY)
    filter_dict = {
        "property": "Date",
        "date": {"equals": yesterday_date},
    }
    pages = query_pages(client, HEALTH_DB_ID, filter_dict=filter_dict, page_size=1)
    if not pages:
        logger.info("Health DB 無昨日（%s）記錄，跳過健康摘要。", yesterday_date)
        return None

    props = pages[0].get("properties", {})

    def _get_number(prop_name: str) -> Optional[float]:
        """取得 Notion number property 的值。"""
        return props.get(prop_name, {}).get("number")

    steps = _get_number("Steps")
    sleep_hours = _get_number("Sleep Hours")
    sleep_score = _get_number("Sleep Score")
    rhr = _get_number("Resting HR")
    stress = _get_number("Stress Avg")
    body_battery = _get_number("Body Battery")
    calories = _get_number("Active Calories")

    parts: List[str] = []
    if steps is not None:
        parts.append(f"步數 {int(steps):,}")
    if sleep_hours is not None:
        parts.append(f"睡眠 {sleep_hours}h")
    if sleep_score is not None:
        parts.append(f"睡眠分 {int(sleep_score)}")
    if rhr is not None:
        parts.append(f"心率 {int(rhr)}bpm")
    if stress is not None:
        parts.append(f"壓力 {int(stress)}")
    if body_battery is not None:
        parts.append(f"電量 {int(body_battery)}")
    if calories is not None:
        parts.append(f"活動 {int(calories)}kcal")

    if not parts:
        return None

    return "昨日健康：" + " | ".join(parts)


# ── 建立日記頁面 ───────────────────────────────────────────────────────────────


def create_journal_page(
    record_date: str,
    health_summary: Optional[str] = None,
) -> bool:
    """在 Notion Daily Journal DB 建立當日頁面。

    若同日頁面已存在則跳過（防重複寫入）。
    優先嘗試帶入健康摘要（寫入 "Health Summary" 欄位）；
    若該欄位不存在，退回只建立 Name + Date 基本欄位。

    Args:
        record_date: 記錄日期，ISO 格式（例如 ``'2024-01-15'``）。
        health_summary: 健康摘要字串（可選）。

    Returns:
        建立成功（或已存在跳過）回傳 True，失敗回傳 False。
    """
    client = get_notion_client(NOTION_API_KEY)

    # 防重複寫入
    if page_exists_for_date(client, JOURNAL_DB_ID, record_date):
        logger.info("日記頁面已存在（%s），跳過建立。", record_date)
        return True

    # 先嘗試帶健康摘要建立
    if health_summary:
        properties_with_summary: Dict[str, object] = {
            "Name": prop_title(f"Daily Journal {record_date}"),
            "Date": prop_date(record_date),
            "Health Summary": prop_rich_text(health_summary),
        }
        result = create_page(client, JOURNAL_DB_ID, properties_with_summary)
        if result is not None:
            logger.info("日記頁面已建立（含健康摘要）：%s", record_date)
            return True

        # Health Summary 欄位可能不存在，退回基本建立
        logger.warning(
            "帶健康摘要建立失敗（可能 DB 無 'Health Summary' 欄位），"
            "退回只建立 Name + Date..."
        )

    # 基本欄位建立（Name + Date）
    base_properties: Dict[str, object] = {
        "Name": prop_title(f"Daily Journal {record_date}"),
        "Date": prop_date(record_date),
    }
    result = create_page(client, JOURNAL_DB_ID, base_properties)
    if result is not None:
        logger.info("日記頁面已建立（基本欄位）：%s", record_date)
        return True

    logger.error("日記頁面建立失敗：%s", record_date)
    return False


# ── 主程式 ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """每日日記自動化主程式。"""
    logger.info("=" * 60)
    logger.info("每日日記腳本啟動")
    logger.info("=" * 60)

    config.validate_config()

    if not JOURNAL_DB_ID or JOURNAL_DB_ID == "placeholder":
        logger.warning(
            "JOURNAL_DB_ID 尚未設定，請在 .env 填入後重新執行。"
        )
        sys.exit(0)

    # 今天建立「今日」日記（讓使用者當天就能看到並填寫）
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    logger.info("日記日期：%s", today)

    # 讀取昨天的健康摘要
    health_summary = fetch_yesterday_health_summary(yesterday)
    if health_summary:
        logger.info("昨日健康摘要：%s", health_summary)
    else:
        logger.info("無昨日健康摘要（Health DB 可能尚未寫入）")

    # 建立今天的日記頁面
    success = create_journal_page(today, health_summary)
    if success:
        logger.info("✓ 今日日記頁面已就緒（%s）", today)
    else:
        logger.error("✗ 日記頁面建立失敗")
        sys.exit(1)

    logger.info("每日日記腳本完成")


if __name__ == "__main__":
    main()
