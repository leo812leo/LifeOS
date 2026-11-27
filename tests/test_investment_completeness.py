"""Incomplete market data must never become a published asset total."""

from datetime import date
from typing import Dict, List, Optional
from unittest.mock import MagicMock

import pandas as pd
import pytest

from config import StockInfo
from scripts import investment_tracker as tracker


INVALID_QUOTES = [float("nan"), float("inf"), float("-inf"), 0.0, -1.0]


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Dict[str, MagicMock]:
    """Use synthetic holdings and mock every service and ledger boundary."""
    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 10, 6)

    monkeypatch.setattr(tracker, "date", FrozenDate)
    monkeypatch.setattr(tracker.config, "validate_config", lambda: True)
    monkeypatch.setattr(tracker, "STOCKS_TW", [
        StockInfo(symbol="SAMPLE.TW", name="Sample TW", shares=10, avg_cost=50.0),
    ])
    monkeypatch.setattr(tracker, "STOCKS_US", [
        StockInfo(symbol="SAMPLE.US", name="Sample US", shares=1, avg_cost=150.0),
    ])
    monkeypatch.setattr(tracker, "INVESTMENT_DB_ID", "offline-investment-db")
    mocks = {
        "fetch_price": MagicMock(side_effect=[100.0, 200.0]),
        "fetch_usd_twd_rate_with_fallback": MagicMock(return_value=31.0),
        "get_notion_client": MagicMock(),
        "fetch_prev_portfolio_total": MagicMock(return_value=7000.0),
        "write_to_notion": MagicMock(return_value=True),
        "append_run": MagicMock(),
        "_alert_failure": MagicMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(tracker, name, mock)
    return mocks


@pytest.mark.parametrize("prices", [[None, 200.0], [100.0, None]])
def test_partial_snapshot_stops_before_notion(
    pipeline: Dict[str, MagicMock], prices: List[Optional[float]],
) -> None:
    pipeline["fetch_price"].side_effect = prices
    with pytest.raises(SystemExit) as error:
        tracker.main()
    assert error.value.code == 1
    pipeline["get_notion_client"].assert_not_called()
    pipeline["fetch_prev_portfolio_total"].assert_not_called()
    pipeline["write_to_notion"].assert_not_called()
    pipeline["_alert_failure"].assert_called_once()
    pipeline["append_run"].assert_called_once_with(
        "investment_tracker.py", ok=False, wrote_notion=False,
    )


def test_retry_same_date_publishes_only_complete_total(pipeline: Dict[str, MagicMock]) -> None:
    pipeline["fetch_price"].side_effect = [100.0, None]
    with pytest.raises(SystemExit):
        tracker.main()
    pipeline["fetch_price"].side_effect = [100.0, 200.0]
    tracker.main()
    pipeline["write_to_notion"].assert_called_once()
    portfolio, rate, record_date = pipeline["write_to_notion"].call_args.args
    assert portfolio.errors == []
    assert portfolio.grand_total_twd == pytest.approx(7200.0)
    assert portfolio.daily_change_pct == pytest.approx(2.86)
    assert rate == 31.0
    assert record_date == "2026-10-05"
    assert [call.kwargs["ok"] for call in pipeline["append_run"].call_args_list] == [False, True]


@pytest.mark.parametrize("price", INVALID_QUOTES)
def test_invalid_quote_is_missing_data(pipeline: Dict[str, MagicMock], price: float) -> None:
    pipeline["fetch_price"].side_effect = [100.0, price]
    with pytest.raises(SystemExit) as error:
        tracker.main()
    assert error.value.code == 1
    pipeline["write_to_notion"].assert_not_called()
    pipeline["append_run"].assert_called_once_with(
        "investment_tracker.py", ok=False, wrote_notion=False,
    )


@pytest.mark.parametrize("rate", INVALID_QUOTES)
def test_invalid_fx_stops_before_prices_and_notion(pipeline: Dict[str, MagicMock], rate: float) -> None:
    pipeline["fetch_usd_twd_rate_with_fallback"].return_value = rate
    with pytest.raises(SystemExit) as error:
        tracker.main()
    assert error.value.code == 1
    pipeline["fetch_price"].assert_not_called()
    pipeline["write_to_notion"].assert_not_called()
    pipeline["append_run"].assert_called_once_with(
        "investment_tracker.py", ok=False, wrote_notion=False,
    )


@pytest.mark.parametrize("price", INVALID_QUOTES[1:])
def test_fetch_price_rejects_invalid_latest_close(monkeypatch: pytest.MonkeyPatch, price: float) -> None:
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame({"Close": [100.0, price]})
    monkeypatch.setattr(tracker.yf, "Ticker", MagicMock(return_value=ticker))
    assert tracker.fetch_price("SAMPLE.TW") is None


@pytest.mark.parametrize("rate", INVALID_QUOTES)
def test_invalid_primary_fx_uses_existing_fallback(monkeypatch: pytest.MonkeyPatch, rate: float) -> None:
    monkeypatch.setattr(tracker, "fetch_usd_twd_rate", lambda: rate)
    fallback = MagicMock(return_value=31.0)
    monkeypatch.setattr(tracker, "_fetch_usd_twd_from_frankfurter", fallback)
    assert tracker.fetch_usd_twd_rate_with_fallback() == 31.0
    fallback.assert_called_once()


@pytest.mark.parametrize("rate", INVALID_QUOTES)
def test_invalid_fallback_fx_is_rejected(monkeypatch: pytest.MonkeyPatch, rate: float) -> None:
    response = MagicMock()
    response.json.return_value = {"rates": {"TWD": rate}}
    monkeypatch.setattr(tracker.requests, "get", MagicMock(return_value=response))
    assert tracker._fetch_usd_twd_from_frankfurter() is None


def test_direct_writer_rejects_partial_before_client_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(tracker, "get_notion_client", client)
    portfolio = tracker.PortfolioResult(1000.0, 0.0, 0.0, 1000.0, errors=["SAMPLE.US"])
    assert tracker.write_to_notion(portfolio, 31.0, "2026-10-05") is False
    client.assert_not_called()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_direct_writer_does_not_convert_invalid_total_to_zero(
    monkeypatch: pytest.MonkeyPatch, value: float,
) -> None:
    client = MagicMock()
    monkeypatch.setattr(tracker, "get_notion_client", client)
    portfolio = tracker.PortfolioResult(value, 0.0, 0.0, value)
    assert tracker.write_to_notion(portfolio, 31.0, "2026-10-05") is False
    client.assert_not_called()


def test_aggregate_overflow_stops_before_notion(pipeline: Dict[str, MagicMock]) -> None:
    pipeline["fetch_price"].side_effect = [1e308, 200.0]
    with pytest.raises(SystemExit) as error:
        tracker.main()
    assert error.value.code == 1
    pipeline["get_notion_client"].assert_not_called()
    pipeline["write_to_notion"].assert_not_called()
    pipeline["append_run"].assert_called_once_with(
        "investment_tracker.py", ok=False, wrote_notion=False,
    )
