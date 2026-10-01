"""tests/test_watchdog.py — scripts/watchdog.py（死人開關）測試。

涵蓋 T09 驗收要求的三種情境（皆 mock ``send_alert``，絕不真的推送 Telegram）：

1. Tier-1 管線（health_tracker、daily_adjust）任一支超過 48 小時無成功紀錄 → 告警。
2. run_ledger 只有失敗紀錄、從無成功過 → 告警（``last_success_time`` 回傳 ``None``）。
3. 兩支 Tier-1 管線都在 48 小時內成功過 → 完全不發告警。

另外用一個不 mock ``last_success_time`` 的端到端測試，直接寫入一筆時間戳被
手動改舊的 ``run_ledger.jsonl``，驗證 watchdog 真的會判斷出「過期」並告警
（對應驗收項目：「手動把 ledger 時間戳改舊 → watchdog 發出告警」）。
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.watchdog import (
    _STALE_HOURS,
    _TIER1_SCRIPTS,
    _check_script,
    _send_alert,
    main,
    run_watchdog,
)


# ── _check_script ─────────────────────────────────────────────────────────────


class TestCheckScript:
    def test_alert_when_no_success_ever(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0)
        with patch("scripts.watchdog.last_success_time", return_value=None):
            alert = _check_script("health_tracker.py", now)

        assert alert is not None
        assert "尚無任何成功執行紀錄" in alert

    def test_alert_when_stale_beyond_48_hours(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0)
        stale_success = now - timedelta(hours=_STALE_HOURS + 1)
        with patch("scripts.watchdog.last_success_time", return_value=stale_success):
            alert = _check_script("daily_adjust.py", now)

        assert alert is not None
        assert str(_STALE_HOURS) in alert

    def test_no_alert_when_success_within_48_hours(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0)
        recent_success = now - timedelta(hours=10)
        with patch("scripts.watchdog.last_success_time", return_value=recent_success):
            assert _check_script("health_tracker.py", now) is None

    def test_no_alert_exactly_at_boundary(self) -> None:
        now = datetime(2026, 7, 11, 12, 0, 0)
        just_inside = now - timedelta(hours=_STALE_HOURS - 1)
        with patch("scripts.watchdog.last_success_time", return_value=just_inside):
            assert _check_script("health_tracker.py", now) is None


# ── run_watchdog：三種情境 ─────────────────────────────────────────────────────


class TestRunWatchdogScenarios:
    def test_scenario_no_success_record_triggers_alert(self) -> None:
        """情境一：run_ledger 查無任何成功紀錄（新系統或帳本遺失）→ 兩支管線都告警。"""
        now = datetime(2026, 7, 11, 12, 0, 0)
        with patch("scripts.watchdog.last_success_time", return_value=None), patch(
            "scripts.watchdog._send_alert"
        ) as mock_send_alert:
            alerts = run_watchdog(now=now)

        assert len(alerts) == len(_TIER1_SCRIPTS)
        assert mock_send_alert.call_count == len(_TIER1_SCRIPTS)
        for script in _TIER1_SCRIPTS:
            mock_send_alert.assert_any_call(script, f"已 {_STALE_HOURS}h 未成功執行")

    def test_scenario_stale_success_triggers_alert_for_that_script_only(self) -> None:
        """情境二：只有一支管線的最後成功紀錄超過 48 小時（可能中間全是失敗）→ 只告警那一支。"""
        now = datetime(2026, 7, 11, 12, 0, 0)
        stale = now - timedelta(hours=72)
        recent = now - timedelta(hours=1)

        def fake_last_success(script: str):
            return stale if script == "health_tracker.py" else recent

        with patch(
            "scripts.watchdog.last_success_time", side_effect=fake_last_success
        ), patch("scripts.watchdog._send_alert") as mock_send_alert:
            alerts = run_watchdog(now=now)

        assert len(alerts) == 1
        assert "health_tracker.py" in alerts[0]
        mock_send_alert.assert_called_once_with(
            "health_tracker.py", f"已 {_STALE_HOURS}h 未成功執行"
        )

    def test_scenario_all_normal_sends_no_alert(self) -> None:
        """情境三：兩支 Tier-1 管線都在 48 小時內成功過 → 完全不告警。"""
        now = datetime(2026, 7, 11, 12, 0, 0)
        recent = now - timedelta(hours=5)

        with patch("scripts.watchdog.last_success_time", return_value=recent), patch(
            "scripts.watchdog._send_alert"
        ) as mock_send_alert:
            alerts = run_watchdog(now=now)

        assert alerts == []
        mock_send_alert.assert_not_called()


# ── 端到端：手動把 ledger 時間戳改舊 ───────────────────────────────────────────


class TestWatchdogEndToEndWithRealLedger:
    def test_manually_stale_timestamp_triggers_alert(self, tmp_path, monkeypatch) -> None:
        """驗收項目：手動把 ledger 時間戳改舊 → watchdog 發出告警。

        不 mock ``last_success_time``，只 mock 帳本檔案路徑與 Telegram 推送，
        驗證 watchdog 從真實檔案內容判斷出「過期」的完整路徑。
        """
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        stale_logged_at = (datetime.now() - timedelta(hours=72)).isoformat(
            timespec="seconds"
        )
        fresh_logged_at = datetime.now().isoformat(timespec="seconds")
        lines = [
            {
                "script": "health_tracker.py",
                "date": "2026-07-08",
                "ok": True,
                "wrote_notion": True,
                "logged_at": stale_logged_at,
            },
            {
                "script": "daily_adjust.py",
                "date": "2026-07-11",
                "ok": True,
                "wrote_notion": False,
                "logged_at": fresh_logged_at,
            },
        ]
        ledger_path.write_text(
            "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
            encoding="utf-8",
        )

        with patch("scripts.watchdog._send_alert") as mock_send_alert:
            alerts = run_watchdog()

        assert len(alerts) == 1
        assert "health_tracker.py" in alerts[0]
        mock_send_alert.assert_called_once_with(
            "health_tracker.py", f"已 {_STALE_HOURS}h 未成功執行"
        )


# ── _send_alert：Telegram 邊界 ─────────────────────────────────────────────────


class TestSendAlert:
    def test_forwards_to_telegram_send_alert_with_correct_signature(self) -> None:
        with patch("scripts.telegram_bot.send_alert") as mock_send:
            _send_alert("health_tracker.py", "已 48h 未成功執行")

        mock_send.assert_called_once_with("health_tracker.py", "已 48h 未成功執行")

    def test_telegram_failure_does_not_raise(self) -> None:
        with patch("scripts.telegram_bot.send_alert", side_effect=Exception("boom")):
            _send_alert("health_tracker.py", "已 48h 未成功執行")  # 不應拋出


# ── main() ────────────────────────────────────────────────────────────────────


class TestMain:
    def test_exits_nonzero_when_alerts_triggered(self) -> None:
        with patch("scripts.watchdog.run_watchdog", return_value=["some alert"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1

    def test_does_not_exit_when_no_alerts(self) -> None:
        with patch("scripts.watchdog.run_watchdog", return_value=[]):
            main()  # 不應拋出 SystemExit
