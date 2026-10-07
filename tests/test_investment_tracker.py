"""tests/test_investment_tracker.py — investment_tracker 的單元測試。"""

from datetime import date
from dataclasses import replace as dc_replace
from typing import List
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config import StockInfo
from notion_client_helper import NotionQueryError
from scripts.investment_tracker import (
    PortfolioResult,
    StockPosition,
    _all_stocks_failed,
    _calc_positions,
    _is_weekend,
    calculate_portfolio,
    fetch_price,
    fetch_prev_portfolio_total,
    fetch_usd_twd_rate,
    fetch_usd_twd_rate_with_fallback,
    main,
    write_to_notion,
)


# ── fetch_price ────────────────────────────────────────────────────────────────


class TestFetchPrice:
    def test_returns_price_on_success(self) -> None:
        mock_hist = pd.DataFrame({"Close": [100.0, 105.5]})
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_hist

        with patch("scripts.investment_tracker.yf.Ticker", return_value=mock_ticker):
            result = fetch_price("2330.TW")

        assert result == pytest.approx(105.5)

    def test_returns_none_when_empty_history(self) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()

        with patch("scripts.investment_tracker.yf.Ticker", return_value=mock_ticker):
            result = fetch_price("FAKE")

        assert result is None

    def test_returns_none_on_exception(self) -> None:
        with patch(
            "scripts.investment_tracker.yf.Ticker",
            side_effect=RuntimeError("network error"),
        ):
            result = fetch_price("AAPL")

        assert result is None


# ── fetch_usd_twd_rate ─────────────────────────────────────────────────────────


class TestFetchUsdTwdRate:
    def test_returns_rate_on_success(self) -> None:
        mock_hist = pd.DataFrame({"Close": [31.5, 31.8]})
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_hist

        with patch("scripts.investment_tracker.yf.Ticker", return_value=mock_ticker):
            result = fetch_usd_twd_rate()

        assert result == pytest.approx(31.8)

    def test_returns_none_when_empty(self) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()

        with patch("scripts.investment_tracker.yf.Ticker", return_value=mock_ticker):
            result = fetch_usd_twd_rate()

        assert result is None


# ── fetch_usd_twd_rate_with_fallback ──────────────────────────────────────────


class TestFetchUsdTwdRateWithFallback:
    def test_returns_primary_on_success(self) -> None:
        with patch(
            "scripts.investment_tracker.fetch_usd_twd_rate", return_value=32.1
        ):
            result = fetch_usd_twd_rate_with_fallback()
        assert result == pytest.approx(32.1)

    def test_falls_back_when_primary_fails(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"rates": {"TWD": 31.5}}

        with (
            patch("scripts.investment_tracker.fetch_usd_twd_rate", return_value=None),
            patch(
                "scripts.investment_tracker.requests.get", return_value=mock_resp
            ),
        ):
            result = fetch_usd_twd_rate_with_fallback()

        assert result == pytest.approx(31.5)

    def test_returns_none_when_both_fail(self) -> None:
        with (
            patch("scripts.investment_tracker.fetch_usd_twd_rate", return_value=None),
            patch(
                "scripts.investment_tracker.requests.get",
                side_effect=Exception("timeout"),
            ),
        ):
            result = fetch_usd_twd_rate_with_fallback()

        assert result is None


# ── fetch_prev_portfolio_total ─────────────────────────────────────────────────


class TestFetchPrevPortfolioTotal:
    def test_returns_total_when_found(self) -> None:
        mock_page = {
            "properties": {
                "Total TWD": {"number": 2_500_000.0}
            }
        }
        with patch(
            "scripts.investment_tracker.get_latest_record", return_value=mock_page
        ):
            result = fetch_prev_portfolio_total(MagicMock())
        assert result == pytest.approx(2_500_000.0)

    def test_returns_none_when_no_record(self) -> None:
        with patch(
            "scripts.investment_tracker.get_latest_record", return_value=None
        ):
            result = fetch_prev_portfolio_total(MagicMock())
        assert result is None

    def test_returns_none_when_number_missing(self) -> None:
        mock_page = {"properties": {"Total TWD": {}}}
        with patch(
            "scripts.investment_tracker.get_latest_record", return_value=mock_page
        ):
            result = fetch_prev_portfolio_total(MagicMock())
        assert result is None


# ── _calc_positions ────────────────────────────────────────────────────────────


class TestCalcPositions:
    _SAMPLE_STOCKS: List[StockInfo] = [
        StockInfo(symbol="2330.TW", name="台積電", shares=100, avg_cost=600.0),
        StockInfo(symbol="2317.TW", name="鴻海",   shares=200, avg_cost=100.0),
    ]

    def test_calculates_tw_positions(self) -> None:
        prices = {"2330.TW": 700.0, "2317.TW": 110.0}

        with patch(
            "scripts.investment_tracker.fetch_price",
            side_effect=lambda sym: prices[sym],
        ):
            positions, total, errors = _calc_positions(
                self._SAMPLE_STOCKS, price_in_base=True, usd_twd_rate=31.0
            )

        assert errors == []
        assert len(positions) == 2
        assert total == pytest.approx(700.0 * 100 + 110.0 * 200)

    def test_skips_failed_symbols(self) -> None:
        def mock_fetch(sym: str):
            return None if sym == "2317.TW" else 700.0

        with patch("scripts.investment_tracker.fetch_price", side_effect=mock_fetch):
            positions, total, errors = _calc_positions(
                self._SAMPLE_STOCKS, price_in_base=True, usd_twd_rate=31.0
            )

        assert "2317.TW" in errors
        assert len(positions) == 1
        assert total == pytest.approx(700.0 * 100)

    def test_profit_loss_calculation(self) -> None:
        stocks = [StockInfo(symbol="AAPL", name="Apple", shares=10, avg_cost=150.0)]

        with patch("scripts.investment_tracker.fetch_price", return_value=200.0):
            positions, _, _ = _calc_positions(
                stocks, price_in_base=False, usd_twd_rate=31.0
            )

        pos = positions[0]
        assert pos.profit_loss == pytest.approx((200.0 - 150.0) * 10)


# ── calculate_portfolio ────────────────────────────────────────────────────────


class TestCalculatePortfolio:
    def test_grand_total(self) -> None:
        tw_stock = StockInfo(symbol="2330.TW", name="台積電", shares=100, avg_cost=600.0)
        us_stock = StockInfo(symbol="AAPL", name="Apple", shares=10, avg_cost=150.0)

        def mock_fetch(sym: str):
            return 700.0 if sym == "2330.TW" else 200.0

        with (
            patch("scripts.investment_tracker.STOCKS_TW", [tw_stock]),
            patch("scripts.investment_tracker.STOCKS_US", [us_stock]),
            patch("scripts.investment_tracker.fetch_price", side_effect=mock_fetch),
        ):
            result = calculate_portfolio(usd_twd_rate=31.0)

        expected_tw = 700.0 * 100
        expected_us_usd = 200.0 * 10
        expected_us_twd = expected_us_usd * 31.0

        assert result.tw_total_twd == pytest.approx(expected_tw)
        assert result.us_total_usd == pytest.approx(expected_us_usd)
        assert result.grand_total_twd == pytest.approx(expected_tw + expected_us_twd)

    def test_errors_aggregated(self) -> None:
        tw_stock = StockInfo(symbol="2330.TW", name="台積電", shares=100, avg_cost=600.0)
        us_stock = StockInfo(symbol="AAPL", name="Apple", shares=10, avg_cost=150.0)

        with (
            patch("scripts.investment_tracker.STOCKS_TW", [tw_stock]),
            patch("scripts.investment_tracker.STOCKS_US", [us_stock]),
            patch("scripts.investment_tracker.fetch_price", return_value=None),
        ):
            result = calculate_portfolio(usd_twd_rate=31.0)

        assert "2330.TW" in result.errors
        assert "AAPL" in result.errors

    def test_allocation_pct_sums_to_100(self) -> None:
        """各持股佔比總和應接近 100%。"""
        tw_stock = StockInfo(symbol="2330.TW", name="台積電", shares=100, avg_cost=600.0)
        us_stock = StockInfo(symbol="AAPL", name="Apple", shares=10, avg_cost=150.0)

        def mock_fetch(sym: str):
            return 700.0 if sym == "2330.TW" else 200.0

        with (
            patch("scripts.investment_tracker.STOCKS_TW", [tw_stock]),
            patch("scripts.investment_tracker.STOCKS_US", [us_stock]),
            patch("scripts.investment_tracker.fetch_price", side_effect=mock_fetch),
        ):
            result = calculate_portfolio(usd_twd_rate=31.0)

        all_pcts = [p.allocation_pct for p in result.tw_positions + result.us_positions]
        assert sum(all_pcts) == pytest.approx(100.0, abs=0.1)

    def test_allocation_pct_proportional(self) -> None:
        """台股佔比應反映市值比例。"""
        tw_stock = StockInfo(symbol="2330.TW", name="台積電", shares=1000, avg_cost=0.0)
        us_stock = StockInfo(symbol="AAPL", name="Apple", shares=0, avg_cost=0.0)

        # 台股市值 1000*100=100000，美股 0；台股應佔 100%
        with (
            patch("scripts.investment_tracker.STOCKS_TW", [tw_stock]),
            patch("scripts.investment_tracker.STOCKS_US", [us_stock]),
            patch("scripts.investment_tracker.fetch_price", side_effect=lambda s: 100.0 if s == "2330.TW" else 0.0),
        ):
            result = calculate_portfolio(usd_twd_rate=31.0)

        assert result.tw_positions[0].allocation_pct == pytest.approx(100.0, abs=0.1)

    def test_daily_change_not_set_by_default(self) -> None:
        """calculate_portfolio 本身不設定 daily_change_pct（由 main() 填入）。"""
        tw_stock = StockInfo(symbol="2330.TW", name="台積電", shares=100, avg_cost=600.0)

        with (
            patch("scripts.investment_tracker.STOCKS_TW", [tw_stock]),
            patch("scripts.investment_tracker.STOCKS_US", []),
            patch("scripts.investment_tracker.fetch_price", return_value=700.0),
        ):
            result = calculate_portfolio(usd_twd_rate=31.0)

        assert result.daily_change_pct is None

    def test_dc_replace_sets_daily_change(self) -> None:
        """dc_replace 可正確設定 daily_change_pct 而不修改原物件。"""
        original = PortfolioResult(
            tw_total_twd=100_000.0,
            us_total_usd=0.0,
            us_total_twd=0.0,
            grand_total_twd=100_000.0,
        )
        updated = dc_replace(original, daily_change_pct=2.5, prev_total_twd=97_500.0)

        assert updated.daily_change_pct == pytest.approx(2.5)
        assert updated.prev_total_twd == pytest.approx(97_500.0)
        # 原物件不應被修改
        assert original.daily_change_pct is None


# ── _is_weekend ──────────────────────────────────────────────────────────────


class TestIsWeekend:
    def test_saturday_is_weekend(self) -> None:
        assert _is_weekend(date(2026, 7, 11)) is True  # 週六

    def test_sunday_is_weekend(self) -> None:
        assert _is_weekend(date(2026, 7, 12)) is True  # 週日

    def test_monday_is_not_weekend(self) -> None:
        assert _is_weekend(date(2026, 7, 13)) is False  # 週一


# ── _all_stocks_failed ─────────────────────────────────────────────────────────


class TestAllStocksFailed:
    def test_true_when_no_positions_but_stocks_configured(self) -> None:
        empty = PortfolioResult(
            tw_total_twd=0.0, us_total_usd=0.0, us_total_twd=0.0, grand_total_twd=0.0
        )
        assert _all_stocks_failed(empty, total_configured=1) is True

    def test_false_when_no_stocks_configured(self) -> None:
        """STOCKS_TW/US 皆為空時不算「全部失敗」，只是沒有持股。"""
        empty = PortfolioResult(
            tw_total_twd=0.0, us_total_usd=0.0, us_total_twd=0.0, grand_total_twd=0.0
        )
        assert _all_stocks_failed(empty, total_configured=0) is False

    def test_false_when_at_least_one_position_succeeded(self) -> None:
        pos = StockPosition(
            symbol="0050.TW",
            name="X",
            price=100.0,
            shares=10,
            market_value=1000.0,
            profit_loss=0.0,
        )
        partial = PortfolioResult(
            tw_total_twd=1000.0,
            us_total_usd=0.0,
            us_total_twd=0.0,
            grand_total_twd=1000.0,
            tw_positions=[pos],
        )
        assert _all_stocks_failed(partial, total_configured=2) is False


# ── write_to_notion ────────────────────────────────────────────────────────────


class TestWriteToNotion:
    _PORTFOLIO = PortfolioResult(
        tw_total_twd=100_000.0,
        us_total_usd=0.0,
        us_total_twd=0.0,
        grand_total_twd=100_000.0,
    )

    def test_skips_write_when_dedup_query_fails(self) -> None:
        """Notion 查詢失敗（NotionQueryError）時不可誤判為『沒寫過』而繼續寫入。"""
        with (
            patch(
                "scripts.investment_tracker.get_notion_client",
                return_value=MagicMock(),
            ),
            patch(
                "scripts.investment_tracker.page_exists_for_date",
                side_effect=NotionQueryError("boom"),
            ),
            patch("scripts.investment_tracker.create_page") as mock_create,
        ):
            result = write_to_notion(self._PORTFOLIO, 31.0, "2026-07-13")

        assert result is False
        mock_create.assert_not_called()

    def test_skips_when_already_exists(self) -> None:
        with (
            patch(
                "scripts.investment_tracker.get_notion_client",
                return_value=MagicMock(),
            ),
            patch(
                "scripts.investment_tracker.page_exists_for_date", return_value=True
            ),
            patch("scripts.investment_tracker.create_page") as mock_create,
        ):
            result = write_to_notion(self._PORTFOLIO, 31.0, "2026-07-13")

        assert result is True
        mock_create.assert_not_called()

    def test_creates_page_when_not_exists(self) -> None:
        with (
            patch(
                "scripts.investment_tracker.get_notion_client",
                return_value=MagicMock(),
            ),
            patch(
                "scripts.investment_tracker.page_exists_for_date", return_value=False
            ),
            patch(
                "scripts.investment_tracker.create_page",
                return_value={"id": "page-1"},
            ) as mock_create,
        ):
            result = write_to_notion(self._PORTFOLIO, 31.0, "2026-07-13")

        assert result is True
        mock_create.assert_called_once()


# ── main（週末略過 / 上游全掛 / 部分失敗 / 寫入失敗告警）──────────────────────


def _frozen_date(fixed: date):
    """建立一個 ``datetime.date`` 子類別，``today()`` 恆回傳 ``fixed``。

    用於在測試中凍結 ``date.today()``，不需要額外依賴（如 freezegun）。
    """

    class _Frozen(date):
        @classmethod
        def today(cls) -> date:
            return fixed

    return _Frozen


class TestMainWeekendSkip:
    def test_skips_entirely_when_yesterday_is_weekend(self) -> None:
        """今天是週一 → 昨天是週日（非交易日）→ 完全略過，不抓價也不寫入。"""
        monday = date(2026, 7, 13)
        with (
            patch("scripts.investment_tracker.date", _frozen_date(monday)),
            patch(
                "scripts.investment_tracker.config.validate_config",
                return_value=True,
            ),
            patch(
                "scripts.investment_tracker.fetch_usd_twd_rate_with_fallback"
            ) as mock_fx,
            patch("scripts.investment_tracker.write_to_notion") as mock_write,
            patch("scripts.investment_tracker._alert_failure") as mock_alert,
        ):
            main()

        mock_fx.assert_not_called()
        mock_write.assert_not_called()
        mock_alert.assert_not_called()


class TestMainAllStocksFailed:
    def test_no_write_alerts_and_exits_nonzero(self) -> None:
        """全部標的都拿不到價格 → 不寫 Notion、Telegram 收到告警、exit != 0。"""
        tuesday = date(2026, 7, 14)  # 昨天 = 2026-07-13（週一，非週末）
        failed_portfolio = PortfolioResult(
            tw_total_twd=0.0,
            us_total_usd=0.0,
            us_total_twd=0.0,
            grand_total_twd=0.0,
            errors=["0050.TW"],
        )
        with (
            patch("scripts.investment_tracker.date", _frozen_date(tuesday)),
            patch(
                "scripts.investment_tracker.config.validate_config",
                return_value=True,
            ),
            patch(
                "scripts.investment_tracker.STOCKS_TW",
                [StockInfo(symbol="0050.TW", name="X", shares=1000, avg_cost=1.0)],
            ),
            patch("scripts.investment_tracker.STOCKS_US", []),
            patch(
                "scripts.investment_tracker.fetch_usd_twd_rate_with_fallback",
                return_value=31.0,
            ),
            patch(
                "scripts.investment_tracker.calculate_portfolio",
                return_value=failed_portfolio,
            ),
            patch("scripts.investment_tracker.write_to_notion") as mock_write,
            patch("scripts.investment_tracker._alert_failure") as mock_alert,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code != 0
        mock_write.assert_not_called()
        mock_alert.assert_called_once()
        assert "yfinance" in mock_alert.call_args[0][0]


class TestMainFxFailure:
    def test_alerts_and_exits_nonzero_when_fx_fails(self) -> None:
        """匯率主要 + 備用來源皆失敗 → 告警、exit != 0（既有行為，補上告警）。"""
        tuesday = date(2026, 7, 14)
        with (
            patch("scripts.investment_tracker.date", _frozen_date(tuesday)),
            patch(
                "scripts.investment_tracker.config.validate_config",
                return_value=True,
            ),
            patch(
                "scripts.investment_tracker.fetch_usd_twd_rate_with_fallback",
                return_value=None,
            ),
            patch("scripts.investment_tracker._alert_failure") as mock_alert,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code != 0
        mock_alert.assert_called_once()


class TestMainPartialFailure:
    def test_alerts_and_stops_when_some_stocks_fail(self) -> None:
        """部分標的失敗 → 不發布不完整總額，告警並以非零結束。"""
        tuesday = date(2026, 7, 14)
        partial_portfolio = PortfolioResult(
            tw_total_twd=100_000.0,
            us_total_usd=0.0,
            us_total_twd=0.0,
            grand_total_twd=100_000.0,
            tw_positions=[
                StockPosition(
                    symbol="0050.TW",
                    name="X",
                    price=100.0,
                    shares=1000,
                    market_value=100_000.0,
                    profit_loss=0.0,
                    allocation_pct=100.0,
                )
            ],
            errors=["FAILED.TW"],
        )
        with (
            patch("scripts.investment_tracker.date", _frozen_date(tuesday)),
            patch(
                "scripts.investment_tracker.config.validate_config",
                return_value=True,
            ),
            patch(
                "scripts.investment_tracker.STOCKS_TW",
                [StockInfo(symbol="0050.TW", name="X", shares=1000, avg_cost=1.0)],
            ),
            patch("scripts.investment_tracker.STOCKS_US", []),
            patch(
                "scripts.investment_tracker.fetch_usd_twd_rate_with_fallback",
                return_value=31.0,
            ),
            patch(
                "scripts.investment_tracker.calculate_portfolio",
                return_value=partial_portfolio,
            ),
            patch("scripts.investment_tracker.INVESTMENT_DB_ID", ""),
            patch("scripts.investment_tracker.write_to_notion") as mock_write,
            patch("scripts.investment_tracker._alert_failure") as mock_alert,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        mock_write.assert_not_called()
        mock_alert.assert_called_once()
        assert "FAILED.TW" in mock_alert.call_args[0][0]


class TestMainWriteFailureAlerts:
    def test_alerts_when_write_to_notion_fails(self) -> None:
        """write_to_notion 回傳 False（例如 dedup 查詢失敗）→ 主程式應告警。"""
        tuesday = date(2026, 7, 14)
        ok_portfolio = PortfolioResult(
            tw_total_twd=100_000.0,
            us_total_usd=0.0,
            us_total_twd=0.0,
            grand_total_twd=100_000.0,
            tw_positions=[
                StockPosition(
                    symbol="0050.TW",
                    name="X",
                    price=100.0,
                    shares=1000,
                    market_value=100_000.0,
                    profit_loss=0.0,
                    allocation_pct=100.0,
                )
            ],
        )
        with (
            patch("scripts.investment_tracker.date", _frozen_date(tuesday)),
            patch(
                "scripts.investment_tracker.config.validate_config",
                return_value=True,
            ),
            patch(
                "scripts.investment_tracker.STOCKS_TW",
                [StockInfo(symbol="0050.TW", name="X", shares=1000, avg_cost=1.0)],
            ),
            patch("scripts.investment_tracker.STOCKS_US", []),
            patch(
                "scripts.investment_tracker.fetch_usd_twd_rate_with_fallback",
                return_value=31.0,
            ),
            patch(
                "scripts.investment_tracker.calculate_portfolio",
                return_value=ok_portfolio,
            ),
            patch("scripts.investment_tracker.INVESTMENT_DB_ID", "real-db-id"),
            patch(
                "scripts.investment_tracker.get_notion_client",
                return_value=MagicMock(),
            ),
            patch(
                "scripts.investment_tracker.fetch_prev_portfolio_total",
                return_value=None,
            ),
            patch("scripts.investment_tracker.write_to_notion", return_value=False),
            patch("scripts.investment_tracker._alert_failure") as mock_alert,
            patch("scripts.investment_tracker.append_run") as mock_append_run,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        mock_alert.assert_called_once()
        assert "寫入失敗" in mock_alert.call_args[0][0]
        mock_append_run.assert_called_once_with(
            "investment_tracker.py", ok=False, wrote_notion=False
        )
