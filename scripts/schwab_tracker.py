"""scripts/schwab_tracker.py — Charles Schwab API 美股真實庫存追蹤（骨架）。

啟用步驟：
1. 安裝套件：pip install schwab-py
2. 至 https://developer.schwab.com 建立 App，取得 Client ID 和 Client Secret
3. 在 .env 填入 SCHWAB_CLIENT_ID、SCHWAB_CLIENT_SECRET、SCHWAB_REDIRECT_URI
4. 首次執行時會開啟瀏覽器進行 OAuth 授權（需手動操作一次）
5. Token 儲存於 .schwab_token.json（不上傳 Git）
6. Schwab refresh token 效期 7 天，7 天後需重新授權

注意事項：
- schwab-py 套件文件：https://schwab-py.readthedocs.io/
- Schwab API 目前不支援台股，本腳本僅追蹤美股
- 每個 App 帳號只能連結一個 Schwab 帳戶

執行方式::

    python scripts/schwab_tracker.py
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
    schwab_settings,
)
from notion_client_helper import (
    create_page,
    get_notion_client,
    get_latest_record,
    page_exists_for_date,
    prop_date,
    prop_number,
    prop_rich_text,
    prop_title,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Schwab OAuth token 快取路徑（需排除於 .gitignore）
_SCHWAB_TOKEN_PATH = Path(__file__).resolve().parent.parent / ".schwab_token.json"


# ── Data Transfer Objects ──────────────────────────────────────────────────────


@dataclass
class SchwabPosition:
    """Schwab 讀回的單一美股持股部位。

    Attributes:
        symbol: 美股代碼（例如 'AAPL'）
        description: 股票完整名稱
        quantity: 持股股數
        avg_cost: 平均成本（美元）
        last_price: 最新報價（美元）
        market_value: 市值（美元）
        unrealized_pl: 未實現損益（美元）
    """

    symbol: str
    description: str
    quantity: float
    avg_cost: float
    last_price: float
    market_value: float
    unrealized_pl: float


@dataclass
class SchwabPortfolioResult:
    """Schwab 計算完成的美股投資組合摘要。

    Attributes:
        total_usd: 美股總市值（美元）
        total_twd: 美股總市值（台幣，需帶入匯率換算）
        usd_twd_rate: 使用的 USD/TWD 匯率
        positions: 各持股部位明細
        errors: 讀取失敗的代碼清單
    """

    total_usd: float
    total_twd: float
    usd_twd_rate: float
    positions: List[SchwabPosition] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ── Schwab OAuth 登入 ──────────────────────────────────────────────────────────


def schwab_login():
    """建立 Schwab API Client（OAuth 2.0）。

    首次執行時會開啟瀏覽器完成授權。Token 自動快取於 .schwab_token.json。
    Token 過期後（7 天）需重新執行授權流程。

    TODO: 取消下方 import 和呼叫的註解，填入 .env 後即可使用。

    Returns:
        schwab.client.Client 物件；未設定或失敗時回傳 None。
    """
    if not schwab_settings.is_configured():
        logger.warning(
            "SCHWAB_CLIENT_ID / SCHWAB_CLIENT_SECRET 未設定，跳過 Schwab 追蹤。"
        )
        return None

    try:
        # ── 取消下方註解以啟用 Schwab ─────────────────────────────────────────
        # import schwab
        #
        # client = schwab.auth.client_from_token_file(
        #     token_path=str(_SCHWAB_TOKEN_PATH),
        #     api_key=schwab_settings.client_id,
        #     app_secret=schwab_settings.client_secret,
        # )
        # logger.info("Schwab API 登入成功（使用快取 token）")
        # return client
        # ─────────────────────────────────────────────────────────────────────

        # ── 若 token 不存在，走 OAuth 流程（首次執行才需要）─────────────────
        # if not _SCHWAB_TOKEN_PATH.exists():
        #     logger.info("首次授權：即將開啟瀏覽器，請完成 Schwab 登入...")
        #     client = schwab.auth.client_from_login_flow(
        #         api_key=schwab_settings.client_id,
        #         app_secret=schwab_settings.client_secret,
        #         callback_url=schwab_settings.redirect_uri,
        #         token_path=str(_SCHWAB_TOKEN_PATH),
        #     )
        #     logger.info("Schwab OAuth 授權完成，token 已儲存至 %s", _SCHWAB_TOKEN_PATH)
        #     return client
        # ─────────────────────────────────────────────────────────────────────

        logger.warning("Schwab 登入尚未實作（placeholder）")
        return None

    except Exception as exc:
        logger.error("Schwab 登入失敗：%s", exc)
        return None


# ── 讀取庫存與報價 ─────────────────────────────────────────────────────────────


def fetch_positions(client: object, usd_twd_rate: float) -> SchwabPortfolioResult:
    """從 Schwab API 讀取帳戶持股並計算市值。

    Args:
        client: 已初始化的 schwab.client.Client 物件。
        usd_twd_rate: USD/TWD 匯率（用於換算台幣總值）。

    Returns:
        :class:`SchwabPortfolioResult` 包含各持倉明細與總市值。
    """
    positions: List[SchwabPosition] = []
    errors: List[str] = []

    try:
        # ── 取消下方註解以啟用 ─────────────────────────────────────────────
        # import schwab
        #
        # # 取得所有帳戶的持倉明細
        # resp = client.get_account_numbers()
        # resp.raise_for_status()
        # account_hash = resp.json()[0]["hashValue"]
        #
        # resp = client.get_account(account_hash, fields=[
        #     schwab.client.Client.Account.Fields.POSITIONS
        # ])
        # resp.raise_for_status()
        # account_data = resp.json()
        #
        # for pos in account_data.get("securitiesAccount", {}).get("positions", []):
        #     instrument = pos.get("instrument", {})
        #     symbol = instrument.get("symbol", "")
        #     description = instrument.get("description", symbol)
        #     quantity = float(pos.get("longQuantity", 0))
        #     avg_cost = float(pos.get("averagePrice", 0))
        #     last_price = float(pos.get("marketValue", 0)) / quantity if quantity else 0
        #     market_value = float(pos.get("marketValue", 0))
        #     unrealized_pl = float(pos.get("unrealizedProfitLoss", 0))
        #
        #     positions.append(SchwabPosition(
        #         symbol=symbol,
        #         description=description,
        #         quantity=quantity,
        #         avg_cost=avg_cost,
        #         last_price=last_price,
        #         market_value=market_value,
        #         unrealized_pl=unrealized_pl,
        #     ))
        # ─────────────────────────────────────────────────────────────────────

        logger.warning("Schwab fetch_positions 尚未實作（placeholder）")

    except Exception as exc:
        logger.error("讀取 Schwab 持倉失敗：%s", exc)
        errors.append("Schwab API error")

    total_usd = sum(p.market_value for p in positions)
    total_twd = total_usd * usd_twd_rate

    return SchwabPortfolioResult(
        total_usd=total_usd,
        total_twd=total_twd,
        usd_twd_rate=usd_twd_rate,
        positions=positions,
        errors=errors,
    )


# ── 抓取匯率 ───────────────────────────────────────────────────────────────────


def _fetch_usd_twd_rate() -> float:
    """抓取 USD/TWD 匯率，fallback 到預設值 32.0。

    Returns:
        匯率（float）；所有來源失敗時回傳預設值 32.0。
    """
    try:
        # 複用 investment_tracker 的函式
        from scripts.investment_tracker import fetch_usd_twd_rate_with_fallback
        rate = fetch_usd_twd_rate_with_fallback()
        return rate if rate is not None else 32.0
    except Exception:
        return 32.0


# ── 寫入 Notion ────────────────────────────────────────────────────────────────


def write_to_notion(
    portfolio: SchwabPortfolioResult,
    record_date: str,
) -> bool:
    """將 Schwab 美股持倉結果寫入 Notion Investment DB。

    防重複寫入：同日已存在記錄則跳過。

    Args:
        portfolio: 計算完成的美股組合摘要。
        record_date: 記錄日期，ISO 格式。

    Returns:
        寫入成功（或已存在跳過）回傳 True，失敗回傳 False。
    """
    client = get_notion_client(NOTION_API_KEY)

    if page_exists_for_date(client, INVESTMENT_DB_ID, record_date):
        logger.info("Schwab 投資記錄已存在（%s），跳過寫入。", record_date)
        return True

    note_parts: List[str] = []
    if portfolio.errors:
        note_parts.append(f"失敗：{', '.join(portfolio.errors)}")
    note_parts.append("來源：Schwab")
    note = " | ".join(note_parts)

    properties: Dict[str, object] = {
        "Name":          prop_title(f"Investment {record_date} [Schwab]"),
        "Date":          prop_date(record_date),
        "TW Total":      prop_number(0.0),
        "US Total":      prop_number(portfolio.total_twd),
        "Total TWD":     prop_number(portfolio.total_twd),
        "Exchange Rate": prop_number(portfolio.usd_twd_rate),
        "Notes":         prop_rich_text(note),
    }

    result = create_page(client, INVESTMENT_DB_ID, properties)
    return result is not None


# ── 主程式 ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Schwab 美股追蹤主程式。"""
    logger.info("=" * 60)
    logger.info("Schwab 美股追蹤腳本啟動")
    logger.info("=" * 60)

    config.validate_config()

    if not schwab_settings.is_configured():
        logger.warning(
            "SCHWAB_CLIENT_ID 未設定，請在 .env 填入後重新執行。"
        )
        sys.exit(0)

    client = schwab_login()
    if client is None:
        logger.error("Schwab 登入失敗，腳本終止。")
        sys.exit(1)

    usd_twd_rate = _fetch_usd_twd_rate()
    portfolio = fetch_positions(client, usd_twd_rate)

    record_date = (date.today() - timedelta(days=1)).isoformat()

    logger.info("─ 資料日期：%s", record_date)
    logger.info("─ USD/TWD 匯率：%.4f", usd_twd_rate)
    logger.info("─ 美股總市值：USD %s（NT$ %s）",
                f"{portfolio.total_usd:,.2f}",
                f"{portfolio.total_twd:,.0f}")
    for pos in portfolio.positions:
        logger.info(
            "  · %s %s：%.1f 股，市值 USD %s（損益 %+.2f）",
            pos.symbol,
            pos.description,
            pos.quantity,
            f"{pos.market_value:,.2f}",
            pos.unrealized_pl,
        )

    if INVESTMENT_DB_ID and INVESTMENT_DB_ID != "placeholder":
        success = write_to_notion(portfolio, record_date)
        if success:
            logger.info("✓ 已成功寫入 Notion Investment DB")
        else:
            logger.error("✗ 寫入 Notion 失敗")
    else:
        logger.warning("INVESTMENT_DB_ID 尚未設定，跳過 Notion 寫入。")

    logger.info("Schwab 追蹤腳本完成")


if __name__ == "__main__":
    main()
