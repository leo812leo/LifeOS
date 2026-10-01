"""scripts/shioaji_tracker.py — 永豐金 Shioaji 台股真實庫存追蹤（骨架）。

啟用步驟：
1. 安裝套件：pip install shioaji
2. 在 .env 填入 SHIOAJI_API_KEY 和 SHIOAJI_API_SECRET
3. 若需下單功能，另填 SHIOAJI_CA_PATH、SHIOAJI_CA_PASSWORD、SHIOAJI_PERSON_ID
4. 執行此腳本，實際持股會從 Shioaji API 讀取，不再依賴 config.py 的硬編碼清單

注意事項：
- Shioaji 行情訂閱需要「永豐金期貨/股票」帳號，且每月有免費 API 呼叫次數限制
- 本腳本僅查詢庫存與即時報價，不下單
- 目前 shioaji 套件版本：建議使用 shioaji>=1.1.0

執行方式::

    python scripts/shioaji_tracker.py
"""

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 讓腳本在任何目錄都能正確 import 專案模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from config import (
    INVESTMENT_DB_ID,
    NOTION_API_KEY,
    shioaji_settings,
)
from notion_client_helper import (
    create_page,
    get_notion_client,
    page_exists_for_date,
    prop_date,
    prop_number,
    prop_rich_text,
    prop_title,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ── Data Transfer Objects ──────────────────────────────────────────────────────


@dataclass
class ShioajiPosition:
    """Shioaji 讀回的單一台股持股部位。

    Attributes:
        symbol: 台股代碼（不含 .TW，例如 '2330'）
        name: 股票名稱
        shares: 持股股數（股，非張；1 張 = 1000 股）
        avg_cost: 平均成本（台幣）
        last_price: 最新報價（台幣）
        market_value: 市值（台幣）
        profit_loss: 未實現損益（台幣）
    """

    symbol: str
    name: str
    shares: int
    avg_cost: float
    last_price: float
    market_value: float
    profit_loss: float


@dataclass
class ShioajiPortfolioResult:
    """Shioaji 計算完成的台股投資組合摘要。

    Attributes:
        total_twd: 台股總市值（台幣）
        positions: 各持股部位明細
        errors: 讀取失敗的代碼清單
    """

    total_twd: float
    positions: List[ShioajiPosition] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ── Shioaji 登入 ───────────────────────────────────────────────────────────────


def shioaji_login():
    """登入 Shioaji API 並回傳 api 物件。

    TODO: 取消下方 import 和呼叫的註解，填入正確 API key 後即可使用。

    Returns:
        已登入的 shioaji.Shioaji 物件；未設定或失敗時回傳 None。
    """
    if not shioaji_settings.is_configured():
        logger.warning(
            "SHIOAJI_API_KEY / SHIOAJI_API_SECRET 未設定，跳過 Shioaji 追蹤。"
        )
        return None

    try:
        # ── 取消下方註解以啟用 Shioaji ────────────────────────────────────────
        # import shioaji as sj
        #
        # api = sj.Shioaji(simulation=False)
        # api.login(
        #     api_key=shioaji_settings.api_key,
        #     secret_key=shioaji_settings.api_secret,
        #     fetch_contract=False,   # 只查庫存，不需抓全部合約（加快速度）
        # )
        # logger.info("Shioaji 登入成功")
        # return api
        # ─────────────────────────────────────────────────────────────────────

        logger.warning("Shioaji 登入尚未實作（placeholder）")
        return None

    except Exception as exc:
        logger.error("Shioaji 登入失敗：%s", exc)
        return None


def shioaji_logout(api: object) -> None:
    """登出 Shioaji API。

    Args:
        api: 已登入的 shioaji.Shioaji 物件。
    """
    try:
        # ── 取消下方註解以啟用 ─────────────────────────────────────────────
        # api.logout()
        # logger.info("Shioaji 已登出")
        pass
    except Exception as exc:
        logger.warning("Shioaji 登出失敗：%s", exc)


# ── 讀取庫存與報價 ─────────────────────────────────────────────────────────────


def fetch_positions(api: object) -> List[ShioajiPosition]:
    """從 Shioaji 讀取目前庫存並抓取即時報價。

    Args:
        api: 已登入的 shioaji.Shioaji 物件。

    Returns:
        各持股部位清單；讀取失敗時回傳空清單。
    """
    positions: List[ShioajiPosition] = []

    try:
        # ── 取消下方註解以啟用 ─────────────────────────────────────────────
        # import shioaji as sj
        #
        # # 讀取股票庫存（需先 api.update_status(api.stock_account)）
        # api.update_status(api.stock_account)
        # inventory = api.list_positions(api.stock_account, unit=sj.constant.Unit.Share)
        #
        # for pos in inventory:
        #     symbol = pos.code            # 例如 '2330'
        #     name = pos.code              # 可改用 api.Contracts.Stocks[symbol].name
        #     shares = pos.quantity        # 股數
        #     avg_cost = pos.price         # 平均成本
        #
        #     # 取得即時報價
        #     contract = api.Contracts.Stocks[symbol]
        #     snapshot = api.snapshots([contract])
        #     last_price = snapshot[0].close if snapshot else avg_cost
        #
        #     market_value = last_price * shares
        #     profit_loss = (last_price - avg_cost) * shares
        #
        #     positions.append(ShioajiPosition(
        #         symbol=symbol,
        #         name=name,
        #         shares=shares,
        #         avg_cost=avg_cost,
        #         last_price=last_price,
        #         market_value=market_value,
        #         profit_loss=profit_loss,
        #     ))
        # ─────────────────────────────────────────────────────────────────────

        logger.warning("Shioaji fetch_positions 尚未實作（placeholder）")

    except Exception as exc:
        logger.error("讀取 Shioaji 庫存失敗：%s", exc)

    return positions


def build_portfolio(positions: List[ShioajiPosition]) -> ShioajiPortfolioResult:
    """彙總持股清單為投資組合摘要。

    Args:
        positions: 從 Shioaji 讀回的持股部位清單。

    Returns:
        :class:`ShioajiPortfolioResult` 包含總市值與明細。
    """
    total_twd = sum(p.market_value for p in positions)
    return ShioajiPortfolioResult(total_twd=total_twd, positions=positions)


# ── 寫入 Notion ────────────────────────────────────────────────────────────────


def write_to_notion(
    portfolio: ShioajiPortfolioResult,
    record_date: str,
) -> bool:
    """將 Shioaji 台股持倉結果寫入 Notion Investment DB。

    與 investment_tracker.py 共用同一個 DB，TW Total 欄位代表台股市值（台幣）。
    US Total 欄位留空（此腳本不處理美股）。

    防重複寫入：同日已存在記錄則跳過。

    Args:
        portfolio: 計算完成的台股組合摘要。
        record_date: 記錄日期，ISO 格式。

    Returns:
        寫入成功（或已存在跳過）回傳 True，失敗回傳 False。
    """
    client = get_notion_client(NOTION_API_KEY)

    if page_exists_for_date(client, INVESTMENT_DB_ID, record_date):
        logger.info("Shioaji 投資記錄已存在（%s），跳過寫入。", record_date)
        return True

    note_parts: List[str] = []
    if portfolio.errors:
        note_parts.append(f"失敗：{', '.join(portfolio.errors)}")
    note_parts.append("來源：Shioaji")
    note = " | ".join(note_parts)

    properties: Dict[str, object] = {
        "Name":      prop_title(f"Investment {record_date} [Shioaji]"),
        "Date":      prop_date(record_date),
        "TW Total":  prop_number(portfolio.total_twd),
        "US Total":  prop_number(0.0),
        "Total TWD": prop_number(portfolio.total_twd),
        "Notes":     prop_rich_text(note),
    }

    result = create_page(client, INVESTMENT_DB_ID, properties)
    return result is not None


# ── 主程式 ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Shioaji 台股追蹤主程式。"""
    logger.info("=" * 60)
    logger.info("Shioaji 台股追蹤腳本啟動")
    logger.info("=" * 60)

    config.validate_config()

    if not shioaji_settings.is_configured():
        logger.warning(
            "SHIOAJI_API_KEY 未設定，請在 .env 填入後重新執行。"
        )
        sys.exit(0)

    api = shioaji_login()
    if api is None:
        logger.error("Shioaji 登入失敗，腳本終止。")
        sys.exit(1)

    try:
        positions = fetch_positions(api)
        portfolio = build_portfolio(positions)

        record_date = (date.today() - timedelta(days=1)).isoformat()

        logger.info("─ 資料日期：%s", record_date)
        logger.info("─ 台股總市值：NT$ %s", f"{portfolio.total_twd:,.0f}")
        for pos in portfolio.positions:
            logger.info(
                "  · %s %s：%.1f 股，市值 NT$ %s（損益 %+.0f）",
                pos.symbol,
                pos.name,
                pos.shares,
                f"{pos.market_value:,.0f}",
                pos.profit_loss,
            )

        if INVESTMENT_DB_ID and INVESTMENT_DB_ID != "placeholder":
            success = write_to_notion(portfolio, record_date)
            if success:
                logger.info("✓ 已成功寫入 Notion Investment DB")
            else:
                logger.error("✗ 寫入 Notion 失敗")
    finally:
        shioaji_logout(api)

    logger.info("Shioaji 追蹤腳本完成")


if __name__ == "__main__":
    main()
