"""config.py — 讀取環境變數與定義全域設定。

使用方式::

    from config import settings, STOCKS_TW, STOCKS_US

    if not settings.notion_api_key:
        raise RuntimeError("NOTION_API_KEY 未設定")
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

# 載入 .env 檔案
load_dotenv(Path(__file__).resolve().parent / ".env")


# ── Data Transfer Objects ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class StockInfo:
    """單一持股資訊。

    Attributes:
        symbol: yfinance 代碼（台股加 .TW，例如 '2330.TW'；美股直接填 'AAPL'）
        name: 顯示名稱
        shares: 持股股數
        avg_cost: 平均成本（台股單位：台幣；美股單位：美元）
    """

    symbol: str
    name: str
    shares: int
    avg_cost: float


@dataclass(frozen=True)
class AppSettings:
    """從環境變數讀取的應用程式設定。

    Attributes:
        notion_api_key: Notion Integration Token
        investment_db_id: 投資紀錄 Notion Database ID
        health_db_id: 健康紀錄 Notion Database ID
        activity_db_id: 訓練活動 Notion Database ID（可選）
        garmin_email: Garmin Connect 帳號
        garmin_password: Garmin Connect 密碼
        journal_db_id: 每日日記 Notion Database ID（可選）
        anthropic_api_key: Claude API Key（AI 教練用）
        telegram_bot_token: Telegram Bot Token（推送通知用）
        telegram_chat_id: Telegram Chat ID
        training_plan_db_id: 訓練計劃 Notion Database ID（可選）
    """

    notion_api_key: str
    investment_db_id: str
    health_db_id: str
    garmin_email: str
    garmin_password: str
    activity_db_id: str = ""  # 可選：Activity DB
    journal_db_id: str = ""  # 可選：Daily Journal DB
    anthropic_api_key: str = ""  # 可選：AI 教練
    telegram_bot_token: str = ""  # 可選：Telegram 推送
    telegram_chat_id: str = ""  # 可選：Telegram Chat ID
    training_plan_db_id: str = ""  # 可選：Training Plan DB


@dataclass(frozen=True)
class ShioajiSettings:
    """永豐金 Shioaji API 設定（台股真實庫存追蹤）。

    取得 API 金鑰：登入永豐金 e-Leader 後台 → API 管理 → 建立金鑰。

    Attributes:
        api_key: Shioaji API Key
        api_secret: Shioaji API Secret
        ca_path: 憑證檔路徑（可選，下單功能需要）
        ca_password: 憑證密碼（可選，下單功能需要）
        person_id: 身分證字號（可選，部分進階功能需要）
    """

    api_key: str = ""
    api_secret: str = ""
    ca_path: str = ""
    ca_password: str = ""
    person_id: str = ""

    def is_configured(self) -> bool:
        """檢查是否已設定必要的 API 金鑰。

        Returns:
            api_key 和 api_secret 皆不為空時回傳 True。
        """
        return bool(self.api_key and self.api_secret)


@dataclass(frozen=True)
class SchwabSettings:
    """Charles Schwab API OAuth 設定（美股真實庫存追蹤）。

    取得 OAuth 憑證：登入 Schwab Developer Portal → My Apps → 建立應用程式。
    注意：Schwab API 需要每 7 天重新授權一次（OAuth refresh token 限制）。

    Attributes:
        client_id: Schwab OAuth App Client ID
        client_secret: Schwab OAuth App Client Secret
        redirect_uri: OAuth 重定向 URI（需與 App 設定一致）
    """

    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "https://127.0.0.1"

    def is_configured(self) -> bool:
        """檢查是否已設定 OAuth 憑證。

        Returns:
            client_id 和 client_secret 皆不為空時回傳 True。
        """
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class Glp1Settings:
    """GLP-1 用藥相關設定（單一來源，供 daily_adjust / training_advisor 共用）。

    Attributes:
        injection_day: 注射日（0=週一 ... 6=週日），未設定時預設 4（週五）
        severity: 副作用嚴重度（minimal/mild/moderate/severe），未設定時預設 minimal
    """

    injection_day: int = 4
    severity: str = "minimal"


def get_glp1_settings() -> Glp1Settings:
    """讀取 GLP-1 相關環境變數，回傳呼叫當下建構的設定。

    三支腳本（daily_adjust.py、daily_plan.py〔已除役〕、training_advisor.py）過去各自複製貼上
    ``os.getenv("GLP1_INJECTION_DAY", ...)``，預設值分別漂移成 0 / 4 / 4，未設定
    ``GLP1_INJECTION_DAY`` 時三者判斷的注射日會不一致。此函式作為單一來源統一預設值為 4
    （週五）。刻意設計成「呼叫時才讀取 env」而非模組載入時就固定的常數，以配合既有測試
    對 ``os.environ`` 的 monkeypatch。

    Returns:
        Glp1Settings：呼叫當下依環境變數建構的設定。
    """
    return Glp1Settings(
        injection_day=int(os.getenv("GLP1_INJECTION_DAY", "4")),
        severity=os.getenv("GLP1_SEVERITY", "minimal").lower(),
    )


def get_activity_api_key() -> str:
    """回傳可存取 Activity DB 的 Notion API key（單一來源）。

    Activity DB 位於獨立的 Notion workspace，須以 ``ACTIVITY_NOTION_API_KEY``
    存取；未設定時退回主 workspace 的 ``NOTION_API_KEY``。2026-07 事故教訓：
    daily_adjust / health_tracker（活動同步）/ telegram_bot 各自寫死主 key，
    導致 Activity DB 分享完成後仍連續四天 object_not_found —— 讀寫 Activity DB
    一律經由本函式取 key。刻意設計為呼叫時讀取（配合測試 monkeypatch）。

    Returns:
        API key 字串；兩個環境變數皆未設定時回傳空字串。
    """
    return os.getenv("ACTIVITY_NOTION_API_KEY") or os.getenv("NOTION_API_KEY", "")


def get_vdot() -> float:
    """讀取 VDOT 環境變數，回傳呼叫當下的值（單一來源，T13）。

    系統不自動從訓練數據推算 VDOT。使用者依最近一次比賽或
    測驗結果，每月手動更新 ``.env`` 的 ``VDOT`` 一次即可；未設定時退回
    預設值 38.0（沿用 ``models.AthleteProfile`` 原本的靜態預設）。

    配速表（``utils.vdot_paces.vdot_to_paces``）與訓練 AI prompt 都以此
    函式的回傳值為準，VDOT 改變時配速會跟著連動，不再寫死。

    Returns:
        VDOT 浮點數。

    Raises:
        ValueError: ``VDOT`` 環境變數存在但無法解析為浮點數。
    """
    return float(os.getenv("VDOT", "38.0"))


# ── 讀取環境變數 ───────────────────────────────────────────────────────────────

settings = AppSettings(
    notion_api_key=os.getenv("NOTION_API_KEY", ""),
    investment_db_id=os.getenv("INVESTMENT_DB_ID", ""),
    health_db_id=os.getenv("HEALTH_DB_ID", ""),
    garmin_email=os.getenv("GARMIN_EMAIL", ""),
    garmin_password=os.getenv("GARMIN_PASSWORD", ""),
    activity_db_id=os.getenv("ACTIVITY_DB_ID", ""),
    journal_db_id=os.getenv("JOURNAL_DB_ID", ""),
    anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    training_plan_db_id=os.getenv("TRAINING_PLAN_DB_ID", ""),
)

shioaji_settings = ShioajiSettings(
    api_key=os.getenv("SHIOAJI_API_KEY", ""),
    api_secret=os.getenv("SHIOAJI_API_SECRET", ""),
    ca_path=os.getenv("SHIOAJI_CA_PATH", ""),
    ca_password=os.getenv("SHIOAJI_CA_PASSWORD", ""),
    person_id=os.getenv("SHIOAJI_PERSON_ID", ""),
)

schwab_settings = SchwabSettings(
    client_id=os.getenv("SCHWAB_CLIENT_ID", ""),
    client_secret=os.getenv("SCHWAB_CLIENT_SECRET", ""),
    redirect_uri=os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1"),
)

# 向下相容的模組級別變數（供腳本直接 import 使用）
NOTION_API_KEY = settings.notion_api_key
INVESTMENT_DB_ID = settings.investment_db_id
HEALTH_DB_ID = settings.health_db_id
GARMIN_EMAIL = settings.garmin_email
GARMIN_PASSWORD = settings.garmin_password
ACTIVITY_DB_ID = settings.activity_db_id
JOURNAL_DB_ID = settings.journal_db_id
ANTHROPIC_API_KEY = settings.anthropic_api_key
TELEGRAM_BOT_TOKEN = settings.telegram_bot_token
TELEGRAM_CHAT_ID = settings.telegram_chat_id
TRAINING_PLAN_DB_ID = settings.training_plan_db_id


# ── 台股持股清單 ───────────────────────────────────────────────────────────────
# symbol   : yfinance 代碼（台股加 .TW）
# shares   : 持股股數（例如 1 張 = 1000 股）
# avg_cost : 平均成本（台幣）

# Public export: private holdings are never included.
STOCKS_TW: List[StockInfo] = []

# ── 美股持股清單 ───────────────────────────────────────────────────────────────
# avg_cost : 平均成本（美元）

STOCKS_US: List[StockInfo] = []

# ── 匯率代碼 ───────────────────────────────────────────────────────────────────
FX_USD_TWD = "TWD=X"  # yfinance: 1 USD → TWD


# ── 驗證函式 ───────────────────────────────────────────────────────────────────

def validate_config() -> bool:
    """確認必要的環境變數都已設定。

    遇到未設定的 key 時印出警告，方便快速診斷設定問題。
    JOURNAL_DB_ID 為可選，不影響整體驗證結果。

    Returns:
        若所有必要設定皆已填入則回傳 True，否則回傳 False。
    """
    required = {
        "NOTION_API_KEY":   settings.notion_api_key,
        "INVESTMENT_DB_ID": settings.investment_db_id,
        "HEALTH_DB_ID":     settings.health_db_id,
        "GARMIN_EMAIL":     settings.garmin_email,
        "GARMIN_PASSWORD":  settings.garmin_password,
    }
    missing = [k for k, v in required.items() if not v or v == "placeholder"]
    if missing:
        print(f"[config] ⚠ 以下環境變數尚未設定：{', '.join(missing)}")

    # 可選設定：有設定時印出提示
    if not settings.journal_db_id or settings.journal_db_id == "placeholder":
        print("[config] [info] JOURNAL_DB_ID 未設定，每日日記功能將跳過。")

    return len(missing) == 0
