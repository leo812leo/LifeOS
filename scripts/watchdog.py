"""scripts/watchdog.py — 死人開關（deadman switch）。

系統已被證明的死法是「沉默不運行」：旗艦閉環躺數週、CLAUDE.md 壞三個月沒人
發現。這支腳本讓「沉默」永遠等於「故障」，不能等於「沒話說」。

檢查 Tier-1 管線（``health_tracker.py``、``daily_adjust.py``）的
``logs/run_ledger.jsonl`` 紀錄：任一管線超過 48 小時無成功紀錄，發 Telegram
告警。watchdog 自己保持極簡（無外部依賴、只讀檔案 + 發 Telegram），把「watchdog
自己也會壞」列為已知殘餘風險（單機系統的遞迴監控到此為止）。

建議排程：每日 12:00（避開 07:00 health_tracker / 21:00 investment_tracker
的資料窗）。**本腳本不負責註冊 Windows Scheduled Task** — 依專案慣例，排程
註冊是另外的操作步驟。

執行方式::

    python scripts/watchdog.py
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from utils.logger import get_logger
from utils.run_ledger import last_success_time

logger = get_logger(__name__)

# Tier-1：一旦這兩支管線沉默超過 _STALE_HOURS，代表每日心跳/健康資料可能已經
# 停擺，必須告警（見 docs/tasks/T09-heartbeat-deadman.md）。
_TIER1_SCRIPTS = ("health_tracker.py", "daily_adjust.py")
_STALE_HOURS = 48


def _check_script(script: str, now: datetime) -> Optional[str]:
    """檢查單一腳本是否超過 ``_STALE_HOURS`` 無成功紀錄。

    Args:
        script: 腳本檔名。
        now: 判斷用的「現在」時間（供測試注入固定值）。

    Returns:
        異常時回傳告警文字；一切正常回傳 ``None``。
    """
    last_ok = last_success_time(script)

    if last_ok is None:
        return f"{script} 尚無任何成功執行紀錄（run_ledger 查無資料）"

    elapsed_hours = (now - last_ok).total_seconds() / 3600
    if elapsed_hours >= _STALE_HOURS:
        return (
            f"{script} 已超過 {_STALE_HOURS} 小時未成功執行"
            f"（最後成功時間：{last_ok.isoformat(timespec='minutes')}）"
        )
    return None


def run_watchdog(now: Optional[datetime] = None) -> List[str]:
    """檢查所有 Tier-1 管線，異常時推送 Telegram 告警。

    Args:
        now: 供測試注入固定的「現在」時間；預設 ``datetime.now()``。

    Returns:
        本次觸發的告警文字清單（一切正常時為空清單）。
    """
    logger.info("=" * 60)
    logger.info("死人開關檢查 — %s", (now or datetime.now()).isoformat(timespec="seconds"))
    logger.info("=" * 60)

    current_time = now or datetime.now()
    alerts: List[str] = []

    for script in _TIER1_SCRIPTS:
        alert = _check_script(script, current_time)
        if alert:
            alerts.append(alert)
            logger.warning("🚨 %s", alert)
            _send_alert(script, f"已 {_STALE_HOURS}h 未成功執行")
        else:
            logger.info("✅ %s 正常", script)

    if not alerts:
        logger.info("死人開關：Tier-1 管線皆正常。")

    return alerts


def _send_alert(script_name: str, error_message: str) -> None:
    """推送死人開關告警到 Telegram。Telegram 未設定或推送失敗都不拋出例外。"""
    try:
        from scripts.telegram_bot import send_alert

        send_alert(script_name, error_message)
    except Exception as exc:
        logger.warning("Telegram 告警推送失敗（忽略）：%s", exc)


def main() -> None:
    alerts = run_watchdog()
    if alerts:
        sys.exit(1)


if __name__ == "__main__":
    main()
