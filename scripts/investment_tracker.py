"""scripts/investment_tracker.py — 每日投資組合追蹤。

功能：

1. 從 yfinance 抓取台股、美股收盤價
2. 抓取 USD/TWD 即時匯率（附備用來源）
3. 計算各持股市值、未實現損益、佔比
4. 與前一天比較計算日變動 %
5. 防重複寫入（同一天已存在則跳過）
6. 寫入 Notion Investment DB

執行方式::

    python scripts/investment_tracker.py
"""

import math
import sys
from dataclasses import dataclass, field, replace as dc_replace
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 讓腳本在任何目錄都能正確 import 專案模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import yfinance as yf

import config
from config import (
    FX_USD_TWD,
    INVESTMENT_DB_ID,
    NOTION_API_KEY,
    STOCKS_TW,
    STOCKS_US,
    StockInfo,
)
from notion_client_helper import (
    Client,
    NotionQueryError,
    create_page,
    get_latest_record,
    get_notion_client,
    page_exists_for_date,
    prop_date,
    prop_number,
    prop_rich_text,
    prop_title,
)
from utils.logger import get_logger
from utils.run_ledger import append_run

logger = get_logger(__name__)


# ── Data Transfer Objects ──────────────────────────────────────────────────────


@dataclass
class StockPosition:
    """計算後的單一持股部位。

    Attributes:
        symbol: 股票代碼
        name: 顯示名稱
        price: 最新收盤價（原幣）
        shares: 持股股數
        market_value: 市值（原幣）
        profit_loss: 未實現損益（原幣）
        allocation_pct: 佔總資產比例（%，計算完成後填入）
    """

    symbol: str
    name: str
    price: float
    shares: int
    market_value: float
    profit_loss: float
    allocation_pct: float = 0.0


@dataclass
class PortfolioResult:
    """計算完成的投資組合摘要。

    Attributes:
        tw_total_twd: 台股總市值（台幣）
        us_total_usd: 美股總市值（美元）
        us_total_twd: 美股總市值（台幣）
        grand_total_twd: 全部總市值（台幣）
        tw_positions: 各台股部位明細
        us_positions: 各美股部位明細
        errors: 抓取失敗的代碼清單
        daily_change_pct: 與前一天相比的日變動 %（可選）
        prev_total_twd: 前一天的總資產台幣（可選）
    """

    tw_total_twd: float
    us_total_usd: float
    us_total_twd: float
    grand_total_twd: float
    tw_positions: List[StockPosition] = field(default_factory=list)
    us_positions: List[StockPosition] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    daily_change_pct: Optional[float] = None
    prev_total_twd: Optional[float] = None


# ── 抓取股價 ───────────────────────────────────────────────────────────────────


def _positive_finite(value: Optional[float]) -> bool:
    """Return whether a quote is present, finite and strictly positive."""
    return value is not None and math.isfinite(value) and value > 0


def fetch_price(symbol: str) -> Optional[float]:
    """從 yfinance 取得最新收盤價。

    使用 ``period='5d'`` 並跳過 NaN，確保非交易時段也能取到最近有效收盤價。

    Args:
        symbol: yfinance 股票代碼，例如 ``'2330.TW'`` 或 ``'AAPL'``。

    Returns:
        收盤價（float）；抓取失敗或全為 NaN 時回傳 None。
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d")
        if hist.empty:
            logger.warning("[%s] 無法取得歷史資料（可能休市或代碼錯誤）", symbol)
            return None
        valid = hist["Close"].dropna()
        if valid.empty:
            logger.warning("[%s] 所有收盤價均為 NaN（非交易時段或資料缺漏）", symbol)
            return None
        price = float(valid.iloc[-1])
        if not _positive_finite(price):
            logger.warning("[%s] 收盤價無效，視為資料缺漏", symbol)
            return None
        logger.info("[%s] 收盤價：%.2f", symbol, price)
        return price
    except Exception as exc:
        logger.error("[%s] 抓取股價失敗：%s", symbol, exc)
        return None


def fetch_usd_twd_rate() -> Optional[float]:
    """抓取 USD/TWD 匯率（1 美元 = ? 台幣）from yfinance。

    Returns:
        匯率（float）；失敗時回傳 None。
    """
    try:
        ticker = yf.Ticker(FX_USD_TWD)
        hist = ticker.history(period="5d")
        if hist.empty:
            logger.warning("[%s] 無法取得匯率資料", FX_USD_TWD)
            return None
        valid = hist["Close"].dropna()
        if valid.empty:
            logger.warning("[%s] 匯率資料全為 NaN", FX_USD_TWD)
            return None
        rate = float(valid.iloc[-1])
        if not _positive_finite(rate):
            logger.warning("[%s] 匯率無效，視為資料缺漏", FX_USD_TWD)
            return None
        logger.info("USD/TWD 匯率（yfinance）：%.4f", rate)
        return rate
    except Exception as exc:
        logger.error("抓取匯率失敗：%s", exc)
        return None


def _fetch_usd_twd_from_frankfurter() -> Optional[float]:
    """從 Frankfurter API 抓取 USD/TWD 匯率（備用來源，免費無需 API key）。

    Returns:
        匯率（float）；失敗時回傳 None。
    """
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=TWD",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["rates"]["TWD"])
        if not _positive_finite(rate):
            logger.warning("Frankfurter 備用匯率無效，視為資料缺漏")
            return None
        logger.info("USD/TWD 匯率（Frankfurter 備用）：%.4f", rate)
        return rate
    except Exception as exc:
        logger.error("備用匯率來源（Frankfurter）也失敗：%s", exc)
        return None


def fetch_usd_twd_rate_with_fallback() -> Optional[float]:
    """抓取 USD/TWD 匯率，yfinance 失敗時自動切換備用來源。

    嘗試順序：
    1. yfinance TWD=X
    2. Frankfurter API（免費，無需 API key）

    Returns:
        匯率（float）；所有來源均失敗時回傳 None。
    """
    rate = fetch_usd_twd_rate()
    if _positive_finite(rate):
        return rate

    logger.warning("yfinance 匯率失敗，切換備用來源 Frankfurter...")
    return _fetch_usd_twd_from_frankfurter()


# ── 計算投資組合 ───────────────────────────────────────────────────────────────


def _calc_positions(
    stocks: List[StockInfo],
    price_in_base: bool,
    usd_twd_rate: float,
) -> Tuple[List[StockPosition], float, List[str]]:
    """計算一組持股的部位明細與總市值。

    Args:
        stocks: 持股清單（:class:`~config.StockInfo`）。
        price_in_base: True 表示價格已是台幣（台股）；False 表示美元（美股）。
        usd_twd_rate: USD/TWD 匯率，僅在 ``price_in_base=False`` 時使用。

    Returns:
        ``(positions, total_base, errors)``：

        - ``positions``: 計算成功的 :class:`StockPosition` 清單
        - ``total_base``: 原幣總市值
        - ``errors``: 抓取失敗的代碼清單
    """
    positions: List[StockPosition] = []
    total_base = 0.0
    errors: List[str] = []

    for stock in stocks:
        price = fetch_price(stock.symbol)
        if price is None or not _positive_finite(price):
            errors.append(stock.symbol)
            continue

        market_value = price * stock.shares
        profit_loss = (price - stock.avg_cost) * stock.shares
        total_base += market_value

        positions.append(
            StockPosition(
                symbol=stock.symbol,
                name=stock.name,
                price=price,
                shares=stock.shares,
                market_value=market_value,
                profit_loss=profit_loss,
            )
        )

    return positions, total_base, errors


def calculate_portfolio(usd_twd_rate: float) -> PortfolioResult:
    """計算台股和美股各持股的市值，彙總總資產（台幣），並計算各標的佔比。

    Args:
        usd_twd_rate: USD/TWD 匯率。

    Returns:
        :class:`PortfolioResult` 包含所有市值摘要、明細與佔比。
    """
    tw_positions, tw_total_twd, tw_errors = _calc_positions(
        STOCKS_TW, price_in_base=True, usd_twd_rate=usd_twd_rate
    )
    us_positions, us_total_usd, us_errors = _calc_positions(
        STOCKS_US, price_in_base=False, usd_twd_rate=usd_twd_rate
    )

    us_total_twd = us_total_usd * usd_twd_rate
    grand_total_twd = tw_total_twd + us_total_twd

    # 計算各標的佔總資產比例（使用 dc_replace 建立新物件，避免原地修改）
    if grand_total_twd > 0:
        tw_positions = [
            dc_replace(
                pos,
                allocation_pct=round(pos.market_value / grand_total_twd * 100, 2),
            )
            for pos in tw_positions
        ]
        us_positions = [
            dc_replace(
                pos,
                allocation_pct=round(
                    pos.market_value * usd_twd_rate / grand_total_twd * 100, 2
                ),
            )
            for pos in us_positions
        ]

    return PortfolioResult(
        tw_total_twd=tw_total_twd,
        us_total_usd=us_total_usd,
        us_total_twd=us_total_twd,
        grand_total_twd=grand_total_twd,
        tw_positions=tw_positions,
        us_positions=us_positions,
        errors=tw_errors + us_errors,
    )


# ── 歷史比較 ───────────────────────────────────────────────────────────────────


def fetch_prev_portfolio_total(client: Client) -> Optional[float]:
    """從 Notion Investment DB 讀取最新一筆的總資產（台幣）。

    用於計算與今日相比的日變動 %。

    Args:
        client: 已初始化的 Notion Client。

    Returns:
        上一筆記錄的 ``Total TWD`` 欄位值；查無資料時回傳 None。
    """
    page = get_latest_record(client, INVESTMENT_DB_ID, date_property="Date")
    if page is None:
        return None
    total = (
        page.get("properties", {})
        .get("Total TWD", {})
        .get("number")
    )
    return float(total) if total is not None else None


# ── 寫入 Notion ────────────────────────────────────────────────────────────────


def _valid_totals(portfolio: PortfolioResult) -> bool:
    """Reject invalid aggregate values instead of disguising them as zero."""
    return all(math.isfinite(value) and value >= 0 for value in (
        portfolio.tw_total_twd, portfolio.us_total_usd,
        portfolio.us_total_twd, portfolio.grand_total_twd,
    ))


def write_to_notion(
    portfolio: PortfolioResult,
    usd_twd_rate: float,
    record_date: str,
) -> bool:
    """將投資組合結果寫入 Notion Investment DB。

    具備防重複寫入保護：同日已存在記錄時直接回傳 True 跳過。
    資料缺漏或數值無效時拒絕寫入；Notes 帶入日變動 %、前三大持倉比例。

    Notion DB 欄位（固定，請勿更改）：
        ``Name`` ``Date`` ``Total TWD`` ``TW Total`` ``US Total``
        ``Exchange Rate`` ``Notes``

    Args:
        portfolio: 計算完成的投資組合摘要。
        usd_twd_rate: USD/TWD 匯率。
        record_date: 記錄日期，ISO 格式（例如 ``'2024-01-15'``）。

    Returns:
        寫入成功（或已存在跳過）回傳 True，失敗回傳 False。
    """
    if portfolio.errors or not _positive_finite(usd_twd_rate) or not _valid_totals(portfolio):
        logger.error("投資快照不完整或數值無效，拒絕寫入 Notion。")
        return False

    client = get_notion_client(NOTION_API_KEY)

    # 防重複寫入：同一天已存在則跳過。
    # 查詢本身失敗時（raise_on_error=True）不可誤判為「今天沒寫過」而繼續寫入 —
    # 那樣會在 Notion 暫時性錯誤時造成重複記錄；改為直接放棄本次寫入並回報失敗，
    # 讓呼叫端告警、下次排程重跑時能自然補上。
    try:
        already_exists = page_exists_for_date(
            client, INVESTMENT_DB_ID, record_date, raise_on_error=True
        )
    except NotionQueryError as exc:
        logger.error(
            "查詢 Notion 既有記錄失敗，為避免重複寫入，本次跳過寫入：%s", exc
        )
        return False

    if already_exists:
        logger.info("投資記錄已存在（%s），跳過寫入。", record_date)
        return True

    # 建立 Notes 內容
    note_parts: List[str] = []

    if portfolio.daily_change_pct is not None:
        sign = "+" if portfolio.daily_change_pct >= 0 else ""
        note_parts.append(f"日變動：{sign}{portfolio.daily_change_pct:.2f}%")

    # 列出前 3 大持倉佔比
    all_positions = sorted(
        portfolio.tw_positions + portfolio.us_positions,
        key=lambda p: p.allocation_pct,
        reverse=True,
    )[:3]
    if all_positions:
        alloc_str = " ".join(
            f"{p.name}:{p.allocation_pct:.1f}%" for p in all_positions
        )
        note_parts.append(f"前3：{alloc_str}")

    note = " | ".join(note_parts)

    properties: Dict[str, object] = {
        "Name":          prop_title(f"Investment {record_date}"),
        "Date":          prop_date(record_date),
        "Total TWD":     prop_number(portfolio.grand_total_twd),
        "TW Total":      prop_number(portfolio.tw_total_twd),
        "US Total":      prop_number(portfolio.us_total_twd),
        "Exchange Rate": prop_number(usd_twd_rate),
        "Notes":         prop_rich_text(note),
    }

    result = create_page(client, INVESTMENT_DB_ID, properties)
    return result is not None


# ── 失敗告警 ───────────────────────────────────────────────────────────────────


def _alert_failure(message: str) -> None:
    """推送投資追蹤失敗告警到 Telegram。

    Telegram 未設定或推送本身失敗都不會拋出例外，避免干擾主流程（與
    ``health_tracker.py`` 既有模式一致）。

    Args:
        message: 告警內容（不可包含金鑰、token 等敏感資訊）。
    """
    try:
        from scripts.telegram_bot import safe_send_alert

        safe_send_alert("investment_tracker.py", message)
    except Exception as exc:
        logger.warning("Telegram 警告推送失敗（忽略）：%s", exc)


# ── 主程式 ─────────────────────────────────────────────────────────────────────


def _is_weekend(target: date) -> bool:
    """判斷日期是否為週末（週六、週日）。

    Args:
        target: 欲檢查的日期。

    Returns:
        週六或週日回傳 True，否則 False。
    """
    return target.weekday() >= 5  # 5=Saturday, 6=Sunday


def _all_stocks_failed(portfolio: PortfolioResult, total_configured: int) -> bool:
    """判斷是否「全部標的都拿不到價格」（yfinance 完全無資料）。

    Args:
        portfolio: 計算完成的投資組合摘要。
        total_configured: 設定檔中持股清單的總檔數（``STOCKS_TW`` + ``STOCKS_US``）。

    Returns:
        有設定持股、且台股與美股皆無任何成功抓價的部位時回傳 True。
    """
    return (
        total_configured > 0
        and not portfolio.tw_positions
        and not portfolio.us_positions
    )


def main() -> None:
    """投資追蹤主程式。"""
    logger.info("=" * 60)
    logger.info("投資追蹤腳本啟動")
    logger.info("=" * 60)

    config.validate_config()

    # 前一個交易日（台股盤後資料通常延遲一天）。
    # 若「前一天」落在週末，代表當天沒有新的收盤價（yfinance 仍只會回報上個交易日
    # 的舊資料），寫入會產生誤導性的非交易日紀錄（例如週一執行寫出週日的紀錄，
    # 內容卻是週五的收盤價）。上個交易日的資料已由前一個工作日的執行涵蓋，故直接
    # 略過本次寫入，不視為失敗。
    record_date_obj = date.today() - timedelta(days=1)
    if _is_weekend(record_date_obj):
        logger.info(
            "─ %s 為非交易日（週末），略過本次寫入（上個交易日已由前一次執行記錄）。",
            record_date_obj.isoformat(),
        )
        logger.info("投資追蹤腳本完成（週末略過）")
        append_run("investment_tracker.py", ok=True, wrote_notion=False)
        return
    record_date = record_date_obj.isoformat()

    # 使用備用機制抓取匯率
    usd_twd_rate = fetch_usd_twd_rate_with_fallback()
    if usd_twd_rate is None or not _positive_finite(usd_twd_rate):
        logger.error("無法取得匯率（主要 + 備用來源均失敗），腳本終止。")
        _alert_failure(
            "投資追蹤失敗：無法取得 USD/TWD 匯率（yfinance + Frankfurter 備用來源皆失敗）。"
        )
        append_run("investment_tracker.py", ok=False, wrote_notion=False)
        sys.exit(1)

    portfolio = calculate_portfolio(usd_twd_rate)

    # 全部標的都拿不到價格 → 不寫入毒紀錄（Total TWD=0），告警後以非零結束，
    # 讓 Task Scheduler 記錄失敗，同時避免 dedup 把今天鎖死擋住之後的修正重跑。
    total_configured = len(STOCKS_TW) + len(STOCKS_US)
    if _all_stocks_failed(portfolio, total_configured):
        logger.error("所有持股皆無法取得價格，跳過 Notion 寫入。")
        _alert_failure(
            f"投資追蹤失敗：yfinance 無資料（全部 {total_configured} 檔皆失敗："
            f"{', '.join(portfolio.errors)}）。"
        )
        append_run("investment_tracker.py", ok=False, wrote_notion=False)
        sys.exit(1)

    if portfolio.errors:
        logger.error("部分標的抓取失敗，拒絕發布不完整總額：%s", portfolio.errors)
        _alert_failure(
            f"投資追蹤部分標的失敗：{', '.join(portfolio.errors)}"
            "（本次未寫入總額，請稍後重跑補齊）。"
        )
        append_run("investment_tracker.py", ok=False, wrote_notion=False)
        sys.exit(1)

    if not _valid_totals(portfolio):
        logger.error("投資快照總額無效，跳過 Notion 寫入。")
        _alert_failure("投資追蹤失敗：總額數值無效，本次未寫入，請檢查資料後重跑。")
        append_run("investment_tracker.py", ok=False, wrote_notion=False)
        sys.exit(1)

    # 從 Notion 讀取上一筆資料以計算日變動 %
    if INVESTMENT_DB_ID and INVESTMENT_DB_ID != "placeholder":
        notion_client = get_notion_client(NOTION_API_KEY)
        prev_total = fetch_prev_portfolio_total(notion_client)
        if prev_total is not None and prev_total > 0:
            change_pct = (portfolio.grand_total_twd - prev_total) / prev_total * 100
            portfolio = dc_replace(
                portfolio,
                daily_change_pct=round(change_pct, 2),
                prev_total_twd=prev_total,
            )
            logger.info(
                "─ 前次總資產：NT$ %s", f"{prev_total:,.0f}"
            )

    logger.info("─ 資料日期：%s", record_date)
    logger.info("─ USD/TWD 匯率：%.4f", usd_twd_rate)
    logger.info("─ 台股總市值：NT$ %s", f"{portfolio.tw_total_twd:,.0f}")
    logger.info(
        "─ 美股總市值：USD %s（NT$ %s）",
        f"{portfolio.us_total_usd:,.2f}",
        f"{portfolio.us_total_twd:,.0f}",
    )
    logger.info("─ 總資產（台幣）：NT$ %s", f"{portfolio.grand_total_twd:,.0f}")

    if portfolio.daily_change_pct is not None:
        sign = "+" if portfolio.daily_change_pct >= 0 else ""
        logger.info("─ 日變動：%s%.2f%%", sign, portfolio.daily_change_pct)

    # 印出各標的佔比
    all_positions = sorted(
        portfolio.tw_positions + portfolio.us_positions,
        key=lambda p: p.allocation_pct,
        reverse=True,
    )
    for pos in all_positions:
        logger.info(
            "  · %s %s：%.1f%%（損益 %+.0f）",
            pos.symbol,
            pos.name,
            pos.allocation_pct,
            pos.profit_loss,
        )

    if INVESTMENT_DB_ID and INVESTMENT_DB_ID != "placeholder":
        success = write_to_notion(portfolio, usd_twd_rate, record_date)
        if success:
            logger.info("✓ 已成功寫入 Notion Investment DB")
            append_run("investment_tracker.py", ok=True, wrote_notion=True)
        else:
            logger.error("✗ 寫入 Notion 失敗")
            _alert_failure(f"Notion Investment DB 寫入失敗（日期：{record_date}）。")
            append_run("investment_tracker.py", ok=False, wrote_notion=False)
    else:
        logger.warning("INVESTMENT_DB_ID 尚未設定，跳過 Notion 寫入。")
        append_run("investment_tracker.py", ok=True, wrote_notion=False)

    logger.info("投資追蹤腳本完成")


if __name__ == "__main__":
    main()
