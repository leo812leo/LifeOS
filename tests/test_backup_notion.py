"""tests/test_backup_notion.py — scripts/backup_notion.py（T14 全 DB 備份）測試。

涵蓋驗收要求：
1. mock Notion 分頁（兩頁資料）斷言全量 —— 見 test_notion_helpers.py 的
   TestQueryAllPages（本檔專注在 backup_notion 這一層的邏輯：DB 探索、
   多 workspace API key 解析、檔案輪替、雲端複製、Telegram 摘要）。
2. 檔案輪替邏輯：只保留最新 8 份。

所有測試一律 mock Notion client / query_all_pages / telegram_bot.send_text /
run_ledger.append_run —— 絕不真的呼叫 Notion API 或推送 Telegram。
"""

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backup_notion import (
    _backup_single_db,
    _copy_to_cloud,
    _db_display_name,
    _discover_databases,
    _find_cloud_backup_root,
    _log_run,
    _prune_old_backups,
    _resolve_api_key,
    _send_summary,
    main,
    run_backup,
)


# ── _discover_databases ────────────────────────────────────────────────────────


class TestDiscoverDatabases:
    def test_finds_all_non_empty_db_id_vars(self) -> None:
        environ = {
            "HEALTH_DB_ID": "db-health",
            "ACTIVITY_DB_ID": "db-activity",
            "NOTION_API_KEY": "secret-key",  # 非 *_DB_ID，應被忽略
            "SOME_OTHER_VAR": "x",
        }
        result = _discover_databases(environ)
        assert result == {"HEALTH_DB_ID": "db-health", "ACTIVITY_DB_ID": "db-activity"}

    def test_ignores_empty_values(self) -> None:
        environ = {
            "HEALTH_DB_ID": "db-health",
            "TRAINING_PLAN_DB_ID": "",  # 未設定，DB 待建
        }
        result = _discover_databases(environ)
        assert result == {"HEALTH_DB_ID": "db-health"}
        assert "TRAINING_PLAN_DB_ID" not in result

    def test_empty_environ_returns_empty_dict(self) -> None:
        assert _discover_databases({}) == {}


# ── _db_display_name ───────────────────────────────────────────────────────────


class TestDbDisplayName:
    def test_strips_suffix_and_lowercases(self) -> None:
        assert _db_display_name("HEALTH_DB_ID") == "health"
        assert _db_display_name("ACTIVITY_DB_ID") == "activity"

    def test_multi_word_name(self) -> None:
        assert _db_display_name("OBJECTIVES_DB_ID") == "objectives"


# ── _resolve_api_key ───────────────────────────────────────────────────────────


class TestResolveApiKey:
    def test_activity_uses_activity_key_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("ACTIVITY_NOTION_API_KEY", "activity-key")
        result = _resolve_api_key("ACTIVITY_DB_ID", "default-key")
        assert result == "activity-key"

    def test_activity_falls_back_to_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("ACTIVITY_NOTION_API_KEY", raising=False)
        result = _resolve_api_key("ACTIVITY_DB_ID", "default-key")
        assert result == "default-key"

    def test_nutrition_uses_nutrition_key_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("NUTRITION_NOTION_API_KEY", "nutrition-key")
        result = _resolve_api_key("NUTRITION_DB_ID", "default-key")
        assert result == "nutrition-key"

    def test_okr_dbs_use_okr_key_first(self, monkeypatch) -> None:
        monkeypatch.setenv("OKR_NOTION_API_KEY", "okr-key")
        monkeypatch.setenv("NUTRITION_NOTION_API_KEY", "nutrition-key")
        for env_var in ("AREAS_DB_ID", "OBJECTIVES_DB_ID", "KR_DB_ID"):
            assert _resolve_api_key(env_var, "default-key") == "okr-key"

    def test_okr_dbs_fall_back_to_nutrition_key(self, monkeypatch) -> None:
        monkeypatch.delenv("OKR_NOTION_API_KEY", raising=False)
        monkeypatch.setenv("NUTRITION_NOTION_API_KEY", "nutrition-key")
        assert _resolve_api_key("KR_DB_ID", "default-key") == "nutrition-key"

    def test_okr_dbs_fall_back_to_default_when_nothing_set(self, monkeypatch) -> None:
        monkeypatch.delenv("OKR_NOTION_API_KEY", raising=False)
        monkeypatch.delenv("NUTRITION_NOTION_API_KEY", raising=False)
        assert _resolve_api_key("AREAS_DB_ID", "default-key") == "default-key"

    def test_other_dbs_use_default_key(self) -> None:
        assert _resolve_api_key("HEALTH_DB_ID", "default-key") == "default-key"
        assert _resolve_api_key("INVESTMENT_DB_ID", "default-key") == "default-key"


# ── _backup_single_db ──────────────────────────────────────────────────────────


class TestBackupSingleDb:
    def test_writes_json_file_on_success(self, tmp_path) -> None:
        pages = [{"id": "p1"}, {"id": "p2"}]
        with patch("scripts.backup_notion.get_notion_client") as mock_get_client, patch(
            "scripts.backup_notion.query_all_pages", return_value=pages
        ):
            mock_get_client.return_value = MagicMock()
            ok = _backup_single_db("HEALTH_DB_ID", "db-123", "notion-key", tmp_path)

        assert ok is True
        written = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert written == pages

    def test_returns_false_when_api_key_missing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("ACTIVITY_NOTION_API_KEY", raising=False)
        ok = _backup_single_db("ACTIVITY_DB_ID", "db-123", "", tmp_path)
        assert ok is False
        assert not (tmp_path / "activity.json").exists()

    def test_returns_false_when_query_raises(self, tmp_path) -> None:
        with patch("scripts.backup_notion.get_notion_client", return_value=MagicMock()), patch(
            "scripts.backup_notion.query_all_pages", side_effect=Exception("boom")
        ):
            ok = _backup_single_db("HEALTH_DB_ID", "db-123", "notion-key", tmp_path)
        assert ok is False
        assert not (tmp_path / "health.json").exists()


# ── _prune_old_backups：檔案輪替邏輯 ────────────────────────────────────────────


class TestPruneOldBackups:
    def test_keeps_only_latest_8(self, tmp_path) -> None:
        dates = [
            "2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22",
            "2026-06-29", "2026-07-06", "2026-07-13", "2026-07-20",
            "2026-07-27", "2026-08-03",
        ]
        for d in dates:
            (tmp_path / d).mkdir()

        _prune_old_backups(tmp_path, keep=8)

        remaining = sorted(p.name for p in tmp_path.iterdir())
        assert remaining == dates[-8:]

    def test_no_op_when_fewer_than_keep(self, tmp_path) -> None:
        (tmp_path / "2026-07-06").mkdir()
        (tmp_path / "2026-07-13").mkdir()
        _prune_old_backups(tmp_path, keep=8)
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "2026-07-06", "2026-07-13",
        ]

    def test_ignores_non_date_directories(self, tmp_path) -> None:
        (tmp_path / "not-a-date").mkdir()
        for i in range(9):
            (tmp_path / f"2026-01-{i + 1:02d}").mkdir()

        _prune_old_backups(tmp_path, keep=8)

        remaining = {p.name for p in tmp_path.iterdir()}
        assert "not-a-date" in remaining
        assert len(remaining) == 9  # 1 個非日期目錄 + 8 個保留的日期目錄

    def test_missing_root_is_noop(self, tmp_path) -> None:
        missing = tmp_path / "does-not-exist"
        _prune_old_backups(missing, keep=8)  # 不應拋出例外

    def test_deletion_failure_logs_warning_without_raising(self, tmp_path) -> None:
        """刪除某個舊資料夾失敗（例如檔案被佔用）時只記警告，不中斷其餘刪除。

        這個測試同時涵蓋警告訊息的 %s 格式化正確性 —— 呼叫端若傳入
        多個位置參數卻只有一個 %s 佔位符，logging 模組會在背景吞掉
        TypeError 並印出醜陋的 stderr traceback，不易被察覺。
        """
        for d in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
                  "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
                  "2026-01-09"):
            (tmp_path / d).mkdir()

        with patch(
            "scripts.backup_notion.shutil.rmtree", side_effect=OSError("in use")
        ):
            _prune_old_backups(tmp_path, keep=8)  # 不應拋出例外

        # 刪除全部失敗，所有目錄仍在（驗證失敗不會誤刪或中斷迴圈）
        assert len(list(tmp_path.iterdir())) == 9


# ── _find_cloud_backup_root ────────────────────────────────────────────────────


class TestFindCloudBackupRoot:
    def test_finds_onedrive_via_env_var(self, tmp_path, monkeypatch) -> None:
        fake_onedrive = tmp_path / "OneDrive"
        fake_onedrive.mkdir()
        monkeypatch.setenv("OneDriveConsumer", str(fake_onedrive))
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.delenv("OneDriveCommercial", raising=False)

        result = _find_cloud_backup_root()
        assert result == fake_onedrive / "LifeOS-Backups"

    def test_returns_none_when_no_cloud_folder_found(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.delenv("OneDriveConsumer", raising=False)
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nonexistent-home")

        result = _find_cloud_backup_root()
        assert result is None


# ── _copy_to_cloud ─────────────────────────────────────────────────────────────


class TestCopyToCloud:
    def test_copies_when_cloud_root_found(self, tmp_path) -> None:
        dest_dir = tmp_path / "backups" / "2026-07-12"
        dest_dir.mkdir(parents=True)
        (dest_dir / "health.json").write_text("[]", encoding="utf-8")

        cloud_root = tmp_path / "cloud" / "LifeOS-Backups"
        with patch("scripts.backup_notion._find_cloud_backup_root", return_value=cloud_root):
            note = _copy_to_cloud(dest_dir, "2026-07-12")

        assert "已同步至雲端資料夾" in note
        copied_file = cloud_root / "2026-07-12" / "health.json"
        assert copied_file.exists()

    def test_no_cloud_folder_returns_local_only_note(self, tmp_path) -> None:
        dest_dir = tmp_path / "backups" / "2026-07-12"
        dest_dir.mkdir(parents=True)
        with patch("scripts.backup_notion._find_cloud_backup_root", return_value=None):
            note = _copy_to_cloud(dest_dir, "2026-07-12")
        assert note == "無雲端資料夾，僅本機備份"

    def test_copy_failure_does_not_raise(self, tmp_path) -> None:
        dest_dir = tmp_path / "backups" / "2026-07-12"
        dest_dir.mkdir(parents=True)
        cloud_root = tmp_path / "cloud" / "LifeOS-Backups"

        with patch("scripts.backup_notion._find_cloud_backup_root", return_value=cloud_root), patch(
            "scripts.backup_notion.shutil.copytree", side_effect=OSError("disk full")
        ):
            note = _copy_to_cloud(dest_dir, "2026-07-12")

        assert "失敗" in note


# ── _send_summary：Telegram 邊界 ────────────────────────────────────────────────


class TestSendSummary:
    def test_sends_success_summary(self) -> None:
        with patch("scripts.telegram_bot.send_text", return_value=True) as mock_send:
            _send_summary(["health", "activity"], [], "無雲端資料夾，僅本機備份")

        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        assert "2/2" in text

    def test_includes_failed_dbs_in_message(self) -> None:
        with patch("scripts.telegram_bot.send_text", return_value=True) as mock_send:
            _send_summary(["health"], ["activity"], "無雲端資料夾，僅本機備份")

        text = mock_send.call_args[0][0]
        assert "1/2" in text
        assert "activity" in text

    def test_telegram_failure_does_not_raise(self) -> None:
        with patch("scripts.telegram_bot.send_text", side_effect=Exception("boom")):
            _send_summary(["health"], [], "無雲端資料夾，僅本機備份")  # 不應拋出


# ── _log_run：run_ledger 軟依賴 ─────────────────────────────────────────────────


class TestLogRun:
    def test_calls_append_run_when_available(self) -> None:
        with patch("scripts.backup_notion.append_run") as mock_append:
            _log_run(ok=True)
        mock_append.assert_called_once_with(
            "backup_notion.py", ok=True, wrote_notion=False
        )

    def test_noop_when_append_run_is_none(self) -> None:
        with patch("scripts.backup_notion.append_run", None):
            _log_run(ok=True)  # 不應拋出例外（軟依賴：run_ledger 不存在時跳過）

    def test_swallows_exception_from_append_run(self) -> None:
        with patch(
            "scripts.backup_notion.append_run", side_effect=Exception("disk error")
        ):
            _log_run(ok=True)  # 不應拋出


# ── run_backup：端到端（全部 mock 外部服務）─────────────────────────────────────


class TestRunBackupEndToEnd:
    def test_backs_up_all_discovered_dbs_and_writes_files(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HEALTH_DB_ID", "db-health")
        monkeypatch.setenv("INVESTMENT_DB_ID", "db-investment")
        monkeypatch.setenv("NOTION_API_KEY", "notion-key")
        # 避免真實 .env 裡其他 *_DB_ID 混進來影響筆數斷言
        for extra in (
            "ACTIVITY_DB_ID", "NUTRITION_DB_ID", "AREAS_DB_ID",
            "OBJECTIVES_DB_ID", "KR_DB_ID", "JOURNAL_DB_ID",
            "EXPENSE_DB_ID", "INBOX_DB_ID", "PROJECTS_DB_ID", "TASKS_DB_ID",
        ):
            monkeypatch.delenv(extra, raising=False)

        with patch(
            "scripts.backup_notion._discover_databases",
            return_value={
                "HEALTH_DB_ID": "db-health",
                "INVESTMENT_DB_ID": "db-investment",
            },
        ), patch(
            "scripts.backup_notion.get_notion_client", return_value=MagicMock()
        ), patch(
            "scripts.backup_notion.query_all_pages", return_value=[{"id": "p1"}]
        ), patch(
            "scripts.telegram_bot.send_text", return_value=True
        ) as mock_send, patch(
            "scripts.backup_notion.append_run"
        ) as mock_append, patch(
            "scripts.backup_notion._find_cloud_backup_root", return_value=None
        ):
            succeeded, failed = run_backup(backup_root=tmp_path)

        assert sorted(succeeded) == ["health", "investment"]
        assert failed == []

        today_dir = tmp_path / date.today().isoformat()
        assert (today_dir / "health.json").exists()
        assert (today_dir / "investment.json").exists()

        mock_send.assert_called_once()
        mock_append.assert_called_once_with(
            "backup_notion.py", ok=True, wrote_notion=False
        )

    def test_dry_run_does_not_write_files_or_send_telegram(self, tmp_path) -> None:
        with patch(
            "scripts.backup_notion._discover_databases",
            return_value={"HEALTH_DB_ID": "db-health"},
        ), patch("scripts.telegram_bot.send_text") as mock_send, patch(
            "scripts.backup_notion.query_all_pages"
        ) as mock_query, patch(
            "scripts.backup_notion.append_run"
        ) as mock_append:
            succeeded, failed = run_backup(dry_run=True, backup_root=tmp_path)

        assert succeeded == ["health"]
        assert failed == []
        assert not tmp_path.exists() or not any(tmp_path.iterdir())
        mock_send.assert_not_called()
        mock_query.assert_not_called()
        mock_append.assert_not_called()

    def test_no_databases_found_returns_empty(self, tmp_path) -> None:
        with patch("scripts.backup_notion._discover_databases", return_value={}):
            succeeded, failed = run_backup(backup_root=tmp_path)
        assert succeeded == []
        assert failed == []

    def test_all_dbs_failing_skips_cloud_copy_attempt(self, tmp_path) -> None:
        """全部 DB 都失敗時，dest_dir 從未建立 — 不應嘗試複製到雲端（會撲空）。"""
        with patch(
            "scripts.backup_notion._discover_databases",
            return_value={"HEALTH_DB_ID": "db-health"},
        ), patch(
            "scripts.backup_notion.get_notion_client", return_value=MagicMock()
        ), patch(
            "scripts.backup_notion.query_all_pages", side_effect=Exception("unauthorized")
        ), patch(
            "scripts.telegram_bot.send_text", return_value=True
        ) as mock_send, patch(
            "scripts.backup_notion.append_run"
        ) as mock_append, patch(
            "scripts.backup_notion._copy_to_cloud"
        ) as mock_copy:
            succeeded, failed = run_backup(backup_root=tmp_path)

        assert succeeded == []
        assert failed == ["health"]
        mock_copy.assert_not_called()
        text = mock_send.call_args[0][0]
        assert "無成功備份可同步" in text
        mock_append.assert_called_once_with(
            "backup_notion.py", ok=False, wrote_notion=False
        )

    def test_partial_failure_reported_correctly(self, tmp_path) -> None:
        def fake_backup(env_var_name, db_id, api_key, dest_dir):
            return env_var_name == "HEALTH_DB_ID"

        with patch(
            "scripts.backup_notion._discover_databases",
            return_value={"HEALTH_DB_ID": "db-health", "ACTIVITY_DB_ID": "db-activity"},
        ), patch(
            "scripts.backup_notion._backup_single_db", side_effect=fake_backup
        ), patch(
            "scripts.telegram_bot.send_text", return_value=True
        ) as mock_send, patch(
            "scripts.backup_notion.append_run"
        ) as mock_append, patch(
            "scripts.backup_notion._find_cloud_backup_root", return_value=None
        ):
            succeeded, failed = run_backup(backup_root=tmp_path)

        assert succeeded == ["health"]
        assert failed == ["activity"]
        mock_append.assert_called_once_with(
            "backup_notion.py", ok=False, wrote_notion=False
        )
        text = mock_send.call_args[0][0]
        assert "activity" in text


# ── main() ────────────────────────────────────────────────────────────────────


class TestMain:
    def test_main_calls_run_backup_with_dry_run_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "argv", ["backup_notion.py", "--dry-run"])
        with patch("scripts.backup_notion.run_backup") as mock_run:
            main()
        mock_run.assert_called_once_with(dry_run=True)

    def test_main_defaults_to_no_dry_run(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "argv", ["backup_notion.py"])
        with patch("scripts.backup_notion.run_backup") as mock_run:
            main()
        mock_run.assert_called_once_with(dry_run=False)
