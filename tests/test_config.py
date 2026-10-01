"""tests/test_config.py — config 模組的單元測試。"""

import pytest

from config import (
    AppSettings,
    Glp1Settings,
    StockInfo,
    get_activity_api_key,
    get_glp1_settings,
    get_vdot,
    validate_config,
)


class TestStockInfo:
    """StockInfo dataclass 的測試。"""

    def test_creation(self) -> None:
        stock = StockInfo(symbol="2330.TW", name="台積電", shares=100, avg_cost=650.0)
        assert stock.symbol == "2330.TW"
        assert stock.name == "台積電"
        assert stock.shares == 100
        assert stock.avg_cost == 650.0

    def test_frozen(self) -> None:
        """StockInfo 應為不可變（frozen=True）。"""
        stock = StockInfo(symbol="AAPL", name="Apple", shares=10, avg_cost=175.0)
        with pytest.raises(Exception):
            stock.shares = 999  # type: ignore[misc]

    def test_us_stock(self) -> None:
        stock = StockInfo(symbol="VOO", name="Vanguard S&P500 ETF", shares=8, avg_cost=450.0)
        assert stock.symbol == "VOO"
        assert stock.avg_cost == 450.0


class TestAppSettings:
    """AppSettings dataclass 的測試。"""

    def test_creation(self) -> None:
        s = AppSettings(
            notion_api_key="key",
            investment_db_id="db1",
            health_db_id="db2",
            garmin_email="a@b.com",
            garmin_password="pw",
        )
        assert s.notion_api_key == "key"
        assert s.investment_db_id == "db1"
        assert s.health_db_id == "db2"

    def test_frozen(self) -> None:
        s = AppSettings(
            notion_api_key="k",
            investment_db_id="d",
            health_db_id="h",
            garmin_email="e",
            garmin_password="p",
        )
        with pytest.raises(Exception):
            s.notion_api_key = "changed"  # type: ignore[misc]


class TestValidateConfig:
    """validate_config 的測試。"""

    def test_returns_false_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """環境變數未設定時應回傳 False。"""
        import config as cfg

        monkeypatch.setattr(cfg, "settings", AppSettings(
            notion_api_key="",
            investment_db_id="",
            health_db_id="",
            garmin_email="",
            garmin_password="",
        ))
        assert validate_config() is False

    def test_returns_true_when_all_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import config as cfg

        monkeypatch.setattr(cfg, "settings", AppSettings(
            notion_api_key="key",
            investment_db_id="db1",
            health_db_id="db2",
            garmin_email="a@b.com",
            garmin_password="secret",
        ))
        assert validate_config() is True

    def test_returns_false_for_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import config as cfg

        monkeypatch.setattr(cfg, "settings", AppSettings(
            notion_api_key="placeholder",
            investment_db_id="placeholder",
            health_db_id="placeholder",
            garmin_email="placeholder",
            garmin_password="placeholder",
        ))
        assert validate_config() is False


class TestGlp1Settings:
    """get_glp1_settings 的測試 — 統一 daily_adjust/daily_plan/training_advisor 的預設值來源。"""

    def test_default_injection_day_is_friday_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未設定 GLP1_INJECTION_DAY 時，預設應為 4（週五），三支腳本共用同一預設值。"""
        monkeypatch.delenv("GLP1_INJECTION_DAY", raising=False)
        result = get_glp1_settings()
        assert result.injection_day == 4

    def test_default_severity_is_minimal_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GLP1_SEVERITY", raising=False)
        result = get_glp1_settings()
        assert result.severity == "minimal"

    def test_reads_injection_day_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GLP1_INJECTION_DAY", "2")
        result = get_glp1_settings()
        assert result.injection_day == 2

    def test_reads_severity_from_env_lowercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GLP1_SEVERITY", "MODERATE")
        result = get_glp1_settings()
        assert result.severity == "moderate"

    def test_reads_env_at_call_time_not_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """驗證讀取行為發生在呼叫當下，而非 import config 時就固定 — 才能配合既有測試的
        os.environ monkeypatch。"""
        monkeypatch.delenv("GLP1_INJECTION_DAY", raising=False)
        assert get_glp1_settings().injection_day == 4

        monkeypatch.setenv("GLP1_INJECTION_DAY", "1")
        assert get_glp1_settings().injection_day == 1

    def test_returns_frozen_dataclass(self) -> None:
        result = get_glp1_settings()
        assert isinstance(result, Glp1Settings)
        with pytest.raises(Exception):
            result.injection_day = 0  # type: ignore[misc]


class TestGetVdot:
    """get_vdot 的測試 — VDOT 單一來源（T13：取代寫死 38.0）。"""

    def test_default_is_38_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VDOT", raising=False)
        assert get_vdot() == 38.0

    def test_reads_vdot_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VDOT", "42.5")
        assert get_vdot() == 42.5

    def test_reads_env_at_call_time_not_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VDOT", raising=False)
        assert get_vdot() == 38.0

        monkeypatch.setenv("VDOT", "45")
        assert get_vdot() == 45.0

    def test_invalid_vdot_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VDOT", "not-a-number")
        with pytest.raises(ValueError):
            get_vdot()


class TestGetActivityApiKey:
    """get_activity_api_key（2026-07 事故修復）：Activity DB 專屬 key 的 fallback 鏈。

    事故背景：Activity DB 在獨立 workspace，daily_adjust / health_tracker（活動
    同步）/ telegram_bot 曾寫死主 NOTION_API_KEY，導致 DB 分享完成後仍連續四天
    object_not_found。此類別釘住「ACTIVITY 專屬 key 優先、主 key 為備援」的行為。
    """

    def test_prefers_activity_key_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ACTIVITY_NOTION_API_KEY", "activity-key")
        monkeypatch.setenv("NOTION_API_KEY", "main-key")
        assert get_activity_api_key() == "activity-key"

    def test_falls_back_to_main_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ACTIVITY_NOTION_API_KEY", raising=False)
        monkeypatch.setenv("NOTION_API_KEY", "main-key")
        assert get_activity_api_key() == "main-key"

    def test_empty_activity_key_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ACTIVITY_NOTION_API_KEY", "")
        monkeypatch.setenv("NOTION_API_KEY", "main-key")
        assert get_activity_api_key() == "main-key"

    def test_returns_empty_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ACTIVITY_NOTION_API_KEY", raising=False)
        monkeypatch.delenv("NOTION_API_KEY", raising=False)
        assert get_activity_api_key() == ""

    def test_reads_env_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ACTIVITY_NOTION_API_KEY", raising=False)
        monkeypatch.setenv("NOTION_API_KEY", "main-key")
        assert get_activity_api_key() == "main-key"
        monkeypatch.setenv("ACTIVITY_NOTION_API_KEY", "activity-key")
        assert get_activity_api_key() == "activity-key"
