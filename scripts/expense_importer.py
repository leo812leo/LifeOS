"""scripts/expense_importer.py — 麻布記帳 / CWMoney CSV 匯入 Notion Expense DB。

支援兩個 App 的 CSV 格式，自動偵測類型，統一分類後批次匯入 Notion。

使用方式::

    # 匯入麻布記帳
    python scripts/expense_importer.py --file moneybook_export.csv --source moneybook

    # 匯入 CWMoney
    python scripts/expense_importer.py --file cwmoney_export.csv --source cwmoney

    # 自動偵測格式
    python scripts/expense_importer.py --file expense.csv

    # 乾跑模式（解析但不寫入 Notion）
    python scripts/expense_importer.py --file expense.csv --dry-run
"""

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from notion_client_helper import (
    create_page,
    get_notion_client,
    prop_date,
    prop_number,
    prop_rich_text,
    prop_select,
    prop_title,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ── 分類對應表（統一兩個 App 的分類）────────────────────────────────────────

CATEGORY_MAP: Dict[str, str] = {
    # 餐飲
    "食物": "餐飲", "飲食": "餐飲", "飲料": "餐飲", "外食": "餐飲",
    "早餐": "餐飲", "午餐": "餐飲", "晚餐": "餐飲", "宵夜": "餐飲",
    "Food": "餐飲", "Drinks": "餐飲",
    # 交通
    "交通": "交通", "油費": "交通", "停車": "交通", "計程車": "交通",
    "捷運": "交通", "公車": "交通", "高鐵": "交通", "火車": "交通",
    "Transport": "交通", "Gas": "交通", "Parking": "交通",
    # 娛樂
    "娛樂": "娛樂", "休閒": "娛樂", "電影": "娛樂", "遊戲": "娛樂",
    "Entertainment": "娛樂",
    # 購物
    "購物": "購物", "服飾": "購物", "日用品": "購物", "美妝": "購物",
    "Shopping": "購物", "Clothing": "購物",
    # 生活
    "房租": "生活", "水電": "生活", "水電費": "生活", "瓦斯": "生活",
    "電費": "生活", "水費": "生活", "管理費": "生活",
    "Rent": "生活", "Utilities": "生活",
    # 醫療
    "醫療": "醫療", "保健": "醫療", "藥品": "醫療", "看診": "醫療",
    "Medical": "醫療", "Healthcare": "醫療",
    # 教育
    "教育": "教育", "書籍": "教育", "課程": "教育", "學費": "教育",
    "Education": "教育", "Books": "教育",
    # 通訊
    "電話費": "通訊", "網路費": "通訊", "手機": "通訊",
    "Phone": "通訊", "Internet": "通訊",
    # 保險
    "保險": "保險", "Insurance": "保險",
}

# 帳戶對應（匯入時保留原名，但加入常見映射）
ACCOUNT_MAP: Dict[str, str] = {
    "現金": "現金",
    "Cash": "現金",
    "信用卡": "信用卡",
    "Credit Card": "信用卡",
    "永豐": "永豐",
    "永豐銀行": "永豐",
    "國泰": "國泰",
    "國泰世華": "國泰",
    "玉山": "玉山",
    "中信": "中信",
    "中國信託": "中信",
}


def normalize_category(raw: str) -> str:
    """將 App 原始分類映射到統一分類。"""
    if not raw:
        return "其他"
    return CATEGORY_MAP.get(raw.strip(), "其他")


def normalize_account(raw: str) -> str:
    """將 App 原始帳戶映射到統一名稱。"""
    if not raw:
        return "其他"
    return ACCOUNT_MAP.get(raw.strip(), "其他")


# ── 資料模型 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExpenseRecord:
    """單筆記帳記錄。"""

    date: str  # ISO 格式 YYYY-MM-DD
    amount: float  # 支出為正，收入為負（內部統一）
    category: str
    account: str
    notes: str
    source: str  # "麻布記帳" / "CWMoney" / "手動"
    type: str = "支出"  # "支出" / "收入" / "轉帳"


# ── CSV 解析器 ───────────────────────────────────────────────────────────


def detect_source(filepath: str) -> str:
    """從 CSV 表頭偵測來源 App。"""
    with open(filepath, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return "unknown"

    headers_str = ",".join(headers).lower()
    # CWMoney 特徵：有 Subcategory 或英文欄位
    if "subcategory" in headers_str or "amount" in headers_str.lower():
        return "cwmoney"
    # 麻布記帳特徵：日期/金額/分類/帳戶（中文）
    if "日期" in headers_str and "金額" in headers_str:
        return "moneybook"
    return "unknown"


def parse_moneybook_csv(filepath: str) -> List[ExpenseRecord]:
    """解析麻布記帳 CSV。

    欄位：日期, 入帳日, 金額, 分類, 帳戶, 標籤, 描述
    """
    records: List[ExpenseRecord] = []
    with open(filepath, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                amount = float(row.get("金額", 0))
                if amount == 0:
                    continue
                tx_type = "支出" if amount > 0 else "收入"
                records.append(ExpenseRecord(
                    date=_normalize_date(row.get("日期", "")),
                    amount=abs(amount),
                    category=normalize_category(row.get("分類", "")),
                    account=normalize_account(row.get("帳戶", "")),
                    notes=row.get("描述", "").strip(),
                    source="麻布記帳",
                    type=tx_type,
                ))
            except (ValueError, KeyError) as exc:
                logger.warning("略過無效列：%s（%s）", row, exc)
    logger.info("麻布記帳解析完成：%d 筆", len(records))
    return records


def parse_cwmoney_csv(filepath: str) -> List[ExpenseRecord]:
    """解析 CWMoney CSV。

    欄位：Id, 日期/Date, 金額/Amount, 分類/Category, 子分類/Subcategory,
         帳號/Account, 備註/Notes
    """
    records: List[ExpenseRecord] = []
    with open(filepath, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # 同時支援中英文欄位名
                amount_raw = row.get("金額") or row.get("Amount") or "0"
                amount = float(amount_raw)
                if amount == 0:
                    continue
                tx_type = "支出" if amount > 0 else "收入"
                date_raw = row.get("日期") or row.get("Date") or ""
                category_raw = row.get("分類") or row.get("Category") or ""
                account_raw = row.get("帳號") or row.get("Account") or ""
                notes_raw = row.get("備註") or row.get("Notes") or ""
                records.append(ExpenseRecord(
                    date=_normalize_date(date_raw),
                    amount=abs(amount),
                    category=normalize_category(category_raw),
                    account=normalize_account(account_raw),
                    notes=notes_raw.strip(),
                    source="CWMoney",
                    type=tx_type,
                ))
            except (ValueError, KeyError) as exc:
                logger.warning("略過無效列：%s（%s）", row, exc)
    logger.info("CWMoney 解析完成：%d 筆", len(records))
    return records


def _normalize_date(raw: str) -> str:
    """將各種日期格式正規化為 YYYY-MM-DD。"""
    raw = raw.strip()
    if not raw:
        return ""
    # 去除時間部分
    raw = raw.split(" ")[0].split("T")[0]
    # 處理斜線格式 2026/4/15 → 2026-04-15
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3:
            y, m, d = parts
            return f"{y}-{int(m):02d}-{int(d):02d}"
    return raw


# ── Notion 寫入 ──────────────────────────────────────────────────────────


def write_to_notion(records: List[ExpenseRecord], db_id: str, api_key: str) -> Tuple[int, int]:
    """批次寫入 Notion Expense DB。

    Returns:
        (成功筆數, 失敗筆數)
    """
    client = get_notion_client(api_key)
    success = 0
    failed = 0

    for i, rec in enumerate(records, 1):
        title = f"{rec.date} {rec.category} {rec.amount:.0f}"
        properties = {
            "Name": prop_title(title),
            "Date": prop_date(rec.date),
            "Amount": prop_number(rec.amount),
            "Category": prop_select(rec.category),
            "Account": prop_select(rec.account),
            "Type": prop_select(rec.type),
            "Source": prop_select(rec.source),
        }
        if rec.notes:
            properties["Notes"] = prop_rich_text(rec.notes)

        result = create_page(client, db_id, properties)
        if result is not None:
            success += 1
            if i % 10 == 0:
                logger.info("[%d/%d] 已寫入", i, len(records))
        else:
            failed += 1
            logger.warning("[%d/%d] 寫入失敗：%s", i, len(records), title)

    return success, failed


# ── 主程式 ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="麻布記帳 / CWMoney CSV 匯入工具")
    parser.add_argument("--file", required=True, help="CSV 檔案路徑")
    parser.add_argument(
        "--source",
        choices=["moneybook", "cwmoney", "auto"],
        default="auto",
        help="來源 App（預設自動偵測）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析不寫入 Notion",
    )
    args = parser.parse_args()

    filepath = args.file
    if not Path(filepath).exists():
        logger.error("檔案不存在：%s", filepath)
        sys.exit(1)

    # ── 偵測 / 指定來源 ─────────────────────────────────────────
    source = args.source
    if source == "auto":
        source = detect_source(filepath)
        logger.info("自動偵測來源：%s", source)

    # ── 解析 ──────────────────────────────────────────────────
    if source == "moneybook":
        records = parse_moneybook_csv(filepath)
    elif source == "cwmoney":
        records = parse_cwmoney_csv(filepath)
    else:
        logger.error("無法識別 CSV 格式，請指定 --source moneybook 或 --source cwmoney")
        sys.exit(1)

    if not records:
        logger.warning("沒有解析到任何記錄")
        sys.exit(0)

    # ── 摘要 ──────────────────────────────────────────────────
    total_amount = sum(r.amount for r in records if r.type == "支出")
    income_amount = sum(r.amount for r in records if r.type == "收入")
    logger.info("=" * 50)
    logger.info("解析結果摘要")
    logger.info("=" * 50)
    logger.info("總筆數：%d", len(records))
    logger.info("支出總額：$%.0f", total_amount)
    logger.info("收入總額：$%.0f", income_amount)

    # 分類統計
    category_totals: Dict[str, float] = {}
    for r in records:
        if r.type == "支出":
            category_totals[r.category] = category_totals.get(r.category, 0) + r.amount
    logger.info("分類統計：")
    for cat, total in sorted(category_totals.items(), key=lambda x: -x[1]):
        logger.info("  %s：$%.0f", cat, total)

    if args.dry_run:
        logger.info("乾跑模式，不寫入 Notion。")
        return

    # ── 寫入 Notion ───────────────────────────────────────────
    api_key = os.getenv("NOTION_API_KEY", "")
    db_id = os.getenv("EXPENSE_DB_ID", "")
    if not api_key or not db_id:
        logger.error("NOTION_API_KEY 或 EXPENSE_DB_ID 未設定")
        try:
            from scripts.telegram_bot import safe_send_alert
            safe_send_alert(
                "expense_importer.py",
                "NOTION_API_KEY 或 EXPENSE_DB_ID 未設定，無法寫入 Notion。",
            )
        except Exception:
            pass
        sys.exit(1)

    logger.info("開始寫入 Notion Expense DB...")
    success, failed = write_to_notion(records, db_id, api_key)
    logger.info("=" * 50)
    logger.info("寫入完成：成功 %d 筆 / 失敗 %d 筆", success, failed)

    if failed > 0:
        try:
            from scripts.telegram_bot import safe_send_alert
            safe_send_alert(
                "expense_importer.py",
                f"記帳匯入完成但有 {failed} 筆失敗（成功 {success} 筆）。",
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
