"""tests/test_telegram_bot.py — telegram_bot 互動指令的單元測試。

聚焦驗證 T07 修復：
- ``/rpe``、``/niggle`` 過去只 ``logger.info`` 就回覆「已記錄」，從未寫入 Notion
  （謊報成功）。現在必須實際呼叫 Notion API，且回覆訊息要反映真實結果。
- 找不到當天活動、Notion 未設定、或寫入失敗時，一律誠實回覆並暫存到本機
  ``logs/rpe_pending.jsonl``，絕不謊稱「已記錄」。

Notion API 全部 mock 在 HTTP/API 邊界（比照 tests/test_ai_coach.py 的模式），
不會對真實 Notion 發任何請求。async 指令函式用 ``asyncio.run()`` 直接執行，
避免額外引入 pytest-asyncio 依賴。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.telegram_bot import (
    _append_pending_log,
    _authorized_only,
    _find_today_activity_page,
    _is_authorized_chat,
    _merge_notes,
    _send_message,
    _TELEGRAM_MAX_LEN,
    _truncate_for_telegram,
    cmd_fuel,
    cmd_niggle,
    cmd_rpe,
)


# ── 測試輔助 ─────────────────────────────────────────────────────────────────


def _make_update_and_context(args):
    """建構假的 Telegram ``Update``/``Context`` 物件（reply_text 為 AsyncMock）。"""
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args
    return update, context


def _run(coro):
    return asyncio.run(coro)


def _reply_text(update) -> str:
    assert update.message.reply_text.await_count >= 1
    return update.message.reply_text.await_args.args[0]


@pytest.fixture(autouse=True)
def _configured_env(monkeypatch):
    """預設把 NOTION_API_KEY / ACTIVITY_DB_ID 設好，個別測試可再覆蓋。"""
    monkeypatch.setenv("NOTION_API_KEY", "fake-key")
    monkeypatch.setenv("ACTIVITY_DB_ID", "fake-db-id")


def _today_page(notes: str = "") -> dict:
    props = {"Date": {"date": {"start": "2026-07-11"}}}
    if notes:
        props["Notes"] = {"rich_text": [{"text": {"content": notes}}]}
    else:
        props["Notes"] = {"rich_text": []}
    return {"id": "page-today", "properties": props}


# ── cmd_rpe ──────────────────────────────────────────────────────────────────


class TestCmdRpe:
    def test_no_args_shows_usage(self) -> None:
        update, context = _make_update_and_context([])
        _run(cmd_rpe(update, context))
        assert "用法" in _reply_text(update)

    @pytest.mark.parametrize("bad_value", ["0", "11", "abc"])
    def test_invalid_rpe_rejected(self, bad_value: str) -> None:
        update, context = _make_update_and_context([bad_value])
        _run(cmd_rpe(update, context))
        assert "1-10" in _reply_text(update)

    def test_success_writes_notion_and_replies_honestly(self) -> None:
        update, context = _make_update_and_context(["7", "小腿有點緊"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ) as mock_update:
            _run(cmd_rpe(update, context))

        reply = _reply_text(update)
        assert "已寫入 Notion" in reply
        assert "RPE 7" in reply
        assert "小腿有點緊" in reply

        # RPE number + Notes（含備註）都要送出
        props = mock_update.call_args.kwargs["properties"]
        assert props["RPE"] == {"number": 7}
        assert "小腿有點緊" in props["Notes"]["rich_text"][0]["text"]["content"]

    def test_success_without_notes_does_not_touch_notes_field(self) -> None:
        update, context = _make_update_and_context(["5"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ) as mock_update:
            _run(cmd_rpe(update, context))

        props = mock_update.call_args.kwargs.get("properties")
        assert "Notes" not in props
        assert "已寫入 Notion" in _reply_text(update)

    def test_no_activity_today_falls_back_to_pending_log_and_says_so(self) -> None:
        update, context = _make_update_and_context(["6", "還好"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page", return_value=None
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "scripts.telegram_bot._append_pending_log"
        ) as mock_pending:
            _run(cmd_rpe(update, context))

        reply = _reply_text(update)
        assert "已記錄" not in reply  # 不可謊報成功
        assert "已暫存" in reply
        assert "尚未寫入 Notion" in reply
        mock_pending.assert_called_once()
        assert mock_pending.call_args.args[0]["value"] == 6

    def test_write_failure_replies_failure_not_success(self) -> None:
        update, context = _make_update_and_context(["8"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value=None
        ), patch("scripts.telegram_bot._append_pending_log") as mock_pending:
            _run(cmd_rpe(update, context))

        reply = _reply_text(update)
        assert "寫入失敗" in reply
        assert "已記錄" not in reply
        mock_pending.assert_called_once()

    def test_missing_notion_config_falls_back_to_pending_log(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("NOTION_API_KEY", raising=False)
        monkeypatch.delenv("ACTIVITY_DB_ID", raising=False)
        update, context = _make_update_and_context(["4"])
        with patch("scripts.telegram_bot._append_pending_log") as mock_pending:
            _run(cmd_rpe(update, context))

        reply = _reply_text(update)
        assert "已記錄" not in reply
        assert "尚未寫入 Notion" in reply
        mock_pending.assert_called_once()

    def test_unexpected_exception_replies_failure_and_persists(self) -> None:
        update, context = _make_update_and_context(["7"])
        with patch(
            "notion_client_helper.get_notion_client",
            side_effect=RuntimeError("boom"),
        ), patch("scripts.telegram_bot._append_pending_log") as mock_pending:
            _run(cmd_rpe(update, context))

        reply = _reply_text(update)
        assert "寫入失敗" in reply
        assert "已記錄" not in reply
        mock_pending.assert_called_once()


# ── cmd_niggle ───────────────────────────────────────────────────────────────


class TestCmdNiggle:
    def test_no_args_shows_usage(self) -> None:
        update, context = _make_update_and_context([])
        _run(cmd_niggle(update, context))
        assert "用法" in _reply_text(update)

    @pytest.mark.parametrize("bad_value", ["-1", "6", "x"])
    def test_invalid_niggle_rejected(self, bad_value: str) -> None:
        update, context = _make_update_and_context([bad_value])
        _run(cmd_niggle(update, context))
        assert "0-5" in _reply_text(update)

    @pytest.mark.parametrize("value", ["0", "5"])
    def test_boundary_values_accepted(self, value: str) -> None:
        update, context = _make_update_and_context([value])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ):
            _run(cmd_niggle(update, context))
        assert "已寫入 Notion" in _reply_text(update)

    def test_success_records_niggle_score_and_notes(self) -> None:
        update, context = _make_update_and_context(["4", "左膝"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ) as mock_update:
            _run(cmd_niggle(update, context))

        reply = _reply_text(update)
        assert "已寫入 Notion" in reply
        assert "Niggle 4" in reply
        assert "考慮降低強度或休跑" in reply  # niggle >= 3 警示

        props = mock_update.call_args.kwargs.get("properties")
        assert props["Niggle Score"] == {"number": 4}
        assert "左膝" in props["Notes"]["rich_text"][0]["text"]["content"]

    def test_low_niggle_score_has_no_warning(self) -> None:
        update, context = _make_update_and_context(["1"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ):
            _run(cmd_niggle(update, context))

        assert "考慮降低強度或休跑" not in _reply_text(update)

    def test_no_activity_today_still_shows_high_niggle_warning(self) -> None:
        update, context = _make_update_and_context(["5", "疼痛"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page", return_value=None
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "scripts.telegram_bot._append_pending_log"
        ):
            _run(cmd_niggle(update, context))

        reply = _reply_text(update)
        assert "已記錄" not in reply
        assert "尚未寫入 Notion" in reply
        assert "考慮降低強度或休跑" in reply

    def test_write_failure_replies_failure_not_success(self) -> None:
        update, context = _make_update_and_context(["3"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value=None
        ), patch("scripts.telegram_bot._append_pending_log"):
            _run(cmd_niggle(update, context))

        reply = _reply_text(update)
        assert "寫入失敗" in reply
        assert "已記錄" not in reply


# ── cmd_fuel（T11：長跑補給與腸胃訓練）───────────────────────────────────────


class TestCmdFuel:
    def test_no_args_shows_usage(self) -> None:
        update, context = _make_update_and_context([])
        _run(cmd_fuel(update, context))
        assert "用法" in _reply_text(update)

    def test_single_arg_shows_usage(self) -> None:
        update, context = _make_update_and_context(["45"])
        _run(cmd_fuel(update, context))
        assert "用法" in _reply_text(update)

    @pytest.mark.parametrize("bad_carbs", ["-1", "501", "abc"])
    def test_invalid_carbs_rejected(self, bad_carbs: str) -> None:
        update, context = _make_update_and_context([bad_carbs, "2"])
        _run(cmd_fuel(update, context))
        assert "0-500" in _reply_text(update)

    @pytest.mark.parametrize("bad_gi", ["0", "6", "x"])
    def test_invalid_gi_rejected(self, bad_gi: str) -> None:
        update, context = _make_update_and_context(["45", bad_gi])
        _run(cmd_fuel(update, context))
        assert "1-5" in _reply_text(update)

    def test_success_writes_three_fuel_properties(self) -> None:
        """驗收情境：/fuel 45 2 兩包gel → 三欄位（碳水/GI/計劃）正確送出。"""
        update, context = _make_update_and_context(["45", "2", "兩包gel"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ) as mock_update:
            _run(cmd_fuel(update, context))

        reply = _reply_text(update)
        assert "已寫入 Notion" in reply
        assert "碳水 45g" in reply
        assert "GI 2" in reply
        assert "兩包gel" in reply

        props = mock_update.call_args.kwargs["properties"]
        assert props["Fuel Carbs (g)"] == {"number": 45}
        assert props["GI Score"] == {"number": 2}
        assert props["Fuel Plan"]["rich_text"][0]["text"]["content"] == "兩包gel"

    def test_success_without_notes_skips_fuel_plan_property(self) -> None:
        update, context = _make_update_and_context(["60", "1"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ) as mock_update:
            _run(cmd_fuel(update, context))

        props = mock_update.call_args.kwargs["properties"]
        assert "Fuel Plan" not in props
        assert props["Fuel Carbs (g)"] == {"number": 60}
        assert "已寫入 Notion" in _reply_text(update)

    def test_high_gi_adds_adjustment_hint(self) -> None:
        update, context = _make_update_and_context(["70", "4", "gel太甜想吐"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ):
            _run(cmd_fuel(update, context))

        reply = _reply_text(update)
        assert "GI ≥3" in reply
        assert "降低補給量" in reply

    def test_low_gi_has_no_adjustment_hint(self) -> None:
        update, context = _make_update_and_context(["45", "2"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ):
            _run(cmd_fuel(update, context))

        assert "GI ≥3" not in _reply_text(update)

    def test_no_activity_today_falls_back_to_pending_log(self) -> None:
        update, context = _make_update_and_context(["45", "2", "兩包gel"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page", return_value=None
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "scripts.telegram_bot._append_pending_log"
        ) as mock_pending:
            _run(cmd_fuel(update, context))

        reply = _reply_text(update)
        assert "已暫存" in reply
        assert "尚未寫入 Notion" in reply
        record = mock_pending.call_args.args[0]
        assert record["type"] == "fuel"
        assert record["carbs_g"] == 45
        assert record["gi_score"] == 2

    def test_write_failure_replies_failure_not_success(self) -> None:
        update, context = _make_update_and_context(["45", "2"])
        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value=None
        ), patch("scripts.telegram_bot._append_pending_log") as mock_pending:
            _run(cmd_fuel(update, context))

        reply = _reply_text(update)
        assert "寫入失敗" in reply
        assert "已寫入 Notion" not in reply
        mock_pending.assert_called_once()

    def test_missing_notion_config_falls_back_to_pending_log(self, monkeypatch) -> None:
        monkeypatch.delenv("NOTION_API_KEY", raising=False)
        monkeypatch.delenv("ACTIVITY_DB_ID", raising=False)
        update, context = _make_update_and_context(["45", "2"])
        with patch("scripts.telegram_bot._append_pending_log") as mock_pending:
            _run(cmd_fuel(update, context))

        assert "尚未寫入 Notion" in _reply_text(update)
        mock_pending.assert_called_once()

    def test_unexpected_exception_replies_failure_and_persists(self) -> None:
        update, context = _make_update_and_context(["45", "2"])
        with patch(
            "notion_client_helper.get_notion_client",
            side_effect=RuntimeError("boom"),
        ), patch("scripts.telegram_bot._append_pending_log") as mock_pending:
            _run(cmd_fuel(update, context))

        assert "寫入失敗" in _reply_text(update)
        mock_pending.assert_called_once()

    def test_non_whitelisted_chat_is_ignored(self) -> None:
        """/fuel 也要套 T08 白名單：非本人來源不查 Notion、不回覆。"""
        update = MagicMock()
        update.effective_chat.id = 999999999
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["45", "2"]
        wrapped = _authorized_only(cmd_fuel)

        with patch("notion_client_helper.get_notion_client") as mock_client:
            _run(wrapped(update, context))

        mock_client.assert_not_called()
        update.message.reply_text.assert_not_awaited()


# ── T08: chat_id 白名單（輪詢模式授權）───────────────────────────────────────


def _make_update_with_chat(chat_id):
    """建構一個只設定 ``effective_chat.id`` 的假 Update（reply_text 為 AsyncMock）。"""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture(autouse=True)
def _configured_chat_id(monkeypatch):
    """白名單測試預設把 TELEGRAM_CHAT_ID 設成 111222333，個別測試可覆蓋/清除。"""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111222333")


class TestIsAuthorizedChat:
    def test_matching_chat_id_is_authorized(self) -> None:
        update = _make_update_with_chat(111222333)
        assert _is_authorized_chat(update) is True

    def test_matching_chat_id_as_string_is_authorized(self) -> None:
        # Telegram chat id 有時會以字串形式出現在測試/序列化資料中。
        update = _make_update_with_chat("111222333")
        assert _is_authorized_chat(update) is True

    def test_mismatched_chat_id_is_rejected(self) -> None:
        update = _make_update_with_chat(999999999)
        assert _is_authorized_chat(update) is False

    def test_missing_telegram_chat_id_env_fails_closed(self, monkeypatch) -> None:
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        update = _make_update_with_chat(111222333)
        assert _is_authorized_chat(update) is False

    def test_missing_effective_chat_is_rejected(self) -> None:
        update = MagicMock()
        update.effective_chat = None
        assert _is_authorized_chat(update) is False

    def test_non_numeric_chat_id_is_rejected(self) -> None:
        update = _make_update_with_chat("not-a-number")
        assert _is_authorized_chat(update) is False


class TestAuthorizedOnly:
    def test_whitelisted_chat_calls_handler_normally(self) -> None:
        update = _make_update_with_chat(111222333)
        context = MagicMock()
        inner = AsyncMock()
        wrapped = _authorized_only(inner)

        _run(wrapped(update, context))

        inner.assert_awaited_once_with(update, context)
        update.message.reply_text.assert_not_awaited()

    def test_non_whitelisted_chat_is_ignored_silently(self, caplog) -> None:
        import logging

        update = _make_update_with_chat(999999999)
        context = MagicMock()
        inner = AsyncMock()
        wrapped = _authorized_only(inner)

        with caplog.at_level(logging.WARNING, logger="scripts.telegram_bot"):
            _run(wrapped(update, context))

        # 未白名單來源：不呼叫真正的 handler、不回覆任何內容
        inner.assert_not_awaited()
        update.message.reply_text.assert_not_awaited()
        # 但要留下含來源 chat_id 的 WARNING log
        assert any(
            record.levelno == logging.WARNING and "999999999" in record.getMessage()
            for record in caplog.records
        )

    def test_real_command_handler_rpe_ignored_for_non_whitelisted_chat(self) -> None:
        """整合驗證：真實的 cmd_rpe 包上 _authorized_only 後，非白名單來源
        不會觸發任何 Notion 呼叫（防止讀取個資 / 觸發付費 API）。"""
        update = _make_update_with_chat(999999999)
        context = MagicMock()
        context.args = ["7", "小腿有點緊"]
        wrapped = _authorized_only(cmd_rpe)

        with patch("notion_client_helper.get_notion_client") as mock_client:
            _run(wrapped(update, context))

        mock_client.assert_not_called()
        update.message.reply_text.assert_not_awaited()

    def test_real_command_handler_rpe_runs_for_whitelisted_chat(self) -> None:
        update = _make_update_with_chat(111222333)
        context = MagicMock()
        context.args = ["7", "小腿有點緊"]
        wrapped = _authorized_only(cmd_rpe)

        with patch(
            "scripts.telegram_bot._find_today_activity_page",
            return_value=_today_page(),
        ), patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "notion_client_helper.update_page", return_value={"id": "page-today"}
        ):
            _run(wrapped(update, context))

        reply = _reply_text(update)
        assert "已寫入 Notion" in reply


# ── _merge_notes ─────────────────────────────────────────────────────────────


class TestMergeNotes:
    def test_empty_notes_returns_none(self) -> None:
        assert _merge_notes(_today_page(), "RPE", "") is None

    def test_appends_to_existing_notes(self) -> None:
        page = _today_page(notes="Garmin 自動同步備註")
        result = _merge_notes(page, "RPE", "小腿緊")
        assert result == "Garmin 自動同步備註\n[RPE] 小腿緊"

    def test_no_existing_notes_uses_new_line_only(self) -> None:
        page = _today_page()
        result = _merge_notes(page, "Niggle", "左膝")
        assert result == "[Niggle] 左膝"


# ── _find_today_activity_page ─────────────────────────────────────────────────


class TestFindTodayActivityPage:
    def test_returns_page_when_found(self) -> None:
        with patch(
            "notion_client_helper.query_pages", return_value=[{"id": "page-1"}]
        ) as mock_query:
            result = _find_today_activity_page(MagicMock(), "db-123")
        assert result == {"id": "page-1"}
        filter_arg = mock_query.call_args.kwargs["filter_dict"]
        assert filter_arg["property"] == "Date"
        assert "equals" in filter_arg["date"]

    def test_returns_none_when_no_activity(self) -> None:
        with patch("notion_client_helper.query_pages", return_value=[]):
            result = _find_today_activity_page(MagicMock(), "db-123")
        assert result is None


# ── _append_pending_log ────────────────────────────────────────────────────────


class TestAppendPendingLog:
    def test_writes_jsonl_line(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "rpe_pending.jsonl"
        monkeypatch.setattr("scripts.telegram_bot._RPE_PENDING_LOG", log_path)

        _append_pending_log({"type": "rpe", "date": "2026-07-11", "value": 7, "notes": "小腿緊"})

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "rpe"
        assert record["value"] == 7
        assert "logged_at" in record

    def test_appends_without_overwriting(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "rpe_pending.jsonl"
        monkeypatch.setattr("scripts.telegram_bot._RPE_PENDING_LOG", log_path)

        _append_pending_log({"type": "rpe", "date": "2026-07-10", "value": 5, "notes": ""})
        _append_pending_log({"type": "niggle", "date": "2026-07-11", "value": 4, "notes": "左膝"})

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2


# ── 訊息長度防護（T17）───────────────────────────────────────────────────────


class TestTruncateForTelegram:
    def test_short_text_unchanged(self) -> None:
        text = "☀️ **2026-07-15 週三｜今晚訓練日報**\n**結論**：✅ 維持"
        assert _truncate_for_telegram(text) == text

    def test_exactly_at_limit_unchanged(self) -> None:
        text = "x" * _TELEGRAM_MAX_LEN
        assert _truncate_for_telegram(text) == text

    def test_over_limit_is_truncated_within_bound(self) -> None:
        text = "第一段內容\n" + ("填充文字。" * 2000)
        result = _truncate_for_telegram(text)
        assert len(result) <= _TELEGRAM_MAX_LEN
        assert result.startswith("第一段內容")
        assert "已截斷" in result

    def test_truncation_does_not_leave_unclosed_bold_marker(self) -> None:
        """截斷不能切在 **粗體** 標記中間（否則轉 HTML 後產生未封閉標籤）。"""
        # 刻意在超過截斷預算的位置放一組完整的 **標記**，並在其後塞入大量
        # 沒有配對的 ** 讓截斷點落在標記中間，驗證截斷後不會有奇數個 **。
        filler = "填充文字" * 2000
        text = f"開頭\n{filler}\n**這是一個不該被切一半的粗體標記**\n{filler}"
        result = _truncate_for_telegram(text)
        assert len(result) <= _TELEGRAM_MAX_LEN
        # 截斷發生在換行邊界，任何殘留的 ** 都應該是完整配對（偶數個）。
        stripped = result.replace("…（訊息過長，已截斷；完整內容見 logs/advice_log.jsonl）", "")
        assert stripped.count("**") % 2 == 0

    def test_custom_limit(self) -> None:
        text = "a" * 100
        result = _truncate_for_telegram(text, limit=50)
        assert len(result) <= 50


class TestSendMessageTruncation:
    def test_short_message_sent_as_is(self) -> None:
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ok = _send_message("fake-token", "fake-chat-id", "**結論**：✅ 維持")

        assert ok is True
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        assert len(sent_text) <= _TELEGRAM_MAX_LEN


    def test_long_message_is_truncated_before_sending(self) -> None:
        long_text = "☀️ 訓練日報\n" + ("填充文字。" * 2000)
        assert len(long_text) > _TELEGRAM_MAX_LEN

        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ok = _send_message("fake-token", "fake-chat-id", long_text)

        assert ok is True
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        assert len(sent_text) <= _TELEGRAM_MAX_LEN


class TestDeliveryLogPrivacy:
    @pytest.mark.parametrize("retry_after_parse_error", [False, True])
    def test_transport_exception_does_not_log_token_url(
        self, caplog: pytest.LogCaptureFixture, retry_after_parse_error: bool,
    ) -> None:
        from requests import ConnectionError

        marker = "SYNTHETIC_SECRET_DO_NOT_LOG"
        error = ConnectionError("https://api.telegram.org/bot" + marker + "/sendMessage")
        effects = [error]
        if retry_after_parse_error:
            effects.insert(0, MagicMock(status_code=400, text="cannot parse entities"))
        with patch("requests.post", side_effect=effects):
            assert _send_message("fake-token", "fake-chat-id", "Synthetic message") is False

        assert marker not in caplog.text
        assert "Telegram" in caplog.text

    def test_transport_does_not_log_response_body(self, caplog: pytest.LogCaptureFixture) -> None:
        marker = "SYNTHETIC_PRIVATE_RESPONSE"
        with patch("requests.post", return_value=MagicMock(status_code=503, text=marker)):
            assert _send_message("fake-token", "fake-chat-id", "Synthetic message") is False

        assert marker not in caplog.text
        assert "503" in caplog.text

    def test_safe_alert_does_not_log_exception_text(self, caplog: pytest.LogCaptureFixture) -> None:
        from scripts.telegram_bot import safe_send_alert

        marker = "SYNTHETIC_SECRET_DO_NOT_LOG"
        with patch("scripts.telegram_bot.send_alert", side_effect=RuntimeError(marker)):
            safe_send_alert("synthetic.py", "Synthetic failure")

        assert marker not in caplog.text
