"""tests/test_run_ledger.py — utils/run_ledger.py 的帳本讀寫測試。

涵蓋（T09 驗收）：

1. ``append_run`` 正確寫入一行 JSON（含 script/date/ok/wrote_notion/logged_at）。
2. ``append_run`` 內部例外絕不外洩 — 這支帳本掛在 07:00/21:00 生產管線收尾處，
   帳本本身故障不能讓正常執行的腳本失敗。
3. ``read_recent_runs`` 依腳本名稱過濾、忽略壞行、``limit`` 只取檔案末尾 N 行。
4. ``last_success_time`` 找出最後一次成功（``ok=True``）時間；無成功紀錄回傳 ``None``。
"""

import sys
import tracemalloc
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from utils.run_ledger import append_run, last_success_time, read_recent_runs


# ── append_run ───────────────────────────────────────────────────────────────


class TestAppendRun:
    def test_appends_one_json_line_with_expected_fields(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        append_run(
            "health_tracker.py", ok=True, wrote_notion=True, run_date=date(2026, 7, 11)
        )

        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["script"] == "health_tracker.py"
        assert record["ok"] is True
        assert record["wrote_notion"] is True
        assert record["date"] == "2026-07-11"
        assert "logged_at" in record

    def test_defaults_date_to_today_when_not_given(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        append_run("daily_adjust.py", ok=True, wrote_notion=False)

        record = json.loads(ledger_path.read_text(encoding="utf-8").strip())
        assert record["date"] == date.today().isoformat()

    def test_appends_without_overwriting_previous_lines(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        append_run("health_tracker.py", ok=True, wrote_notion=True)
        append_run("daily_adjust.py", ok=False, wrote_notion=False)

        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_creates_parent_directory_when_missing(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "nested" / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        append_run("health_tracker.py", ok=True, wrote_notion=False)

        assert ledger_path.exists()

    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch) -> None:
        """T09 護欄：append 失敗絕不能拋出例外，只記 warning。

        用一個「檔案」佔住原本該是資料夾的路徑，逼 ``Path.mkdir(exist_ok=True)``
        在 ``append_run`` 內部拋出 ``FileExistsError``，驗證呼叫端完全不受影響。
        """
        blocker_file = tmp_path / "blocker"
        blocker_file.write_text("this is a file, not a directory", encoding="utf-8")
        unwritable_path = blocker_file / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", unwritable_path)

        # 不應拋出任何例外。
        append_run("health_tracker.py", ok=True, wrote_notion=True)


# ── read_recent_runs ─────────────────────────────────────────────────────────


class TestReadRecentRuns:
    def test_returns_empty_list_when_file_missing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", tmp_path / "missing.jsonl")
        assert read_recent_runs() == []

    def test_filters_by_script_name(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)
        append_run("health_tracker.py", ok=True, wrote_notion=True)
        append_run("daily_adjust.py", ok=False, wrote_notion=False)

        records = read_recent_runs(script="health_tracker.py")
        assert len(records) == 1
        assert records[0]["script"] == "health_tracker.py"

    def test_returns_all_scripts_when_script_is_none(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)
        append_run("health_tracker.py", ok=True, wrote_notion=True)
        append_run("daily_adjust.py", ok=False, wrote_notion=False)

        assert len(read_recent_runs()) == 2

    def test_skips_unparseable_and_blank_lines(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger_path.write_text(
            "not json\n\n" + json.dumps({"script": "a.py", "ok": True}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        records = read_recent_runs()
        assert len(records) == 1
        assert records[0]["script"] == "a.py"

    def test_limit_returns_only_tail_lines(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)
        for i in range(5):
            append_run(f"script{i}.py", ok=True, wrote_notion=False)

        records = read_recent_runs(limit=2)
        assert len(records) == 2
        assert records[-1]["script"] == "script4.py"

    @pytest.mark.parametrize("limit", [0, -1, -1000])
    def test_non_positive_limits_return_no_records(
        self, tmp_path, monkeypatch, limit: int
    ) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger_path.write_text(
            json.dumps({"script": "a.py"}) + "\n", encoding="utf-8"
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        assert read_recent_runs(limit=limit) == []

    def test_streams_file_instead_of_loading_all_text(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger_path.write_text(
            "\n".join(json.dumps({"script": f"{i}.py"}) for i in range(3)) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        def fail_read_text(*args, **kwargs) -> None:
            raise AssertionError("reader should stream rather than read the whole file")

        monkeypatch.setattr(Path, "read_text", fail_read_text)
        records = read_recent_runs(limit=2)

        assert [record["script"] for record in records] == ["1.py", "2.py"]

    def test_tail_read_keeps_memory_bounded(self, tmp_path, monkeypatch) -> None:
        """A small tail must not retain the multi-megabyte source file."""
        ledger_path = tmp_path / "run_ledger.jsonl"
        with ledger_path.open("w", encoding="utf-8") as ledger_file:
            for index in range(5000):
                ledger_file.write(
                    json.dumps({"script": f"{index}.py", "padding": "x" * 1024}) + "\n"
                )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        tracemalloc.start()
        try:
            records = read_recent_runs(limit=2)
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert [record["script"] for record in records] == ["4998.py", "4999.py"]
        # The file exceeds 5 MB; 1 MB leaves ample room for buffered IO/JSON.
        assert peak_bytes < 1_000_000

    @pytest.mark.parametrize("script", [None, "a.py"])
    def test_skips_valid_json_values_that_are_not_objects(
        self, tmp_path, monkeypatch, script: Optional[str]
    ) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger_path.write_text(
            "null\n[]\n1\ntrue\n\"text\"\n" + json.dumps({"script": "a.py"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        assert read_recent_runs(script=script) == [{"script": "a.py"}]

    def test_limit_applies_before_filtering_and_skipping_invalid_lines(
        self, tmp_path, monkeypatch
    ) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger_path.write_text(
            json.dumps({"script": "a.py", "old": True}) + "\n"
            + json.dumps({"script": "b.py"}) + "\n"
            + "not-json\n\nnull\n"
            + json.dumps({"script": "a.py", "old": False}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        assert read_recent_runs(script="a.py", limit=5) == [
            {"script": "a.py", "old": False}
        ]

    def test_read_failure_returns_empty_list(self, tmp_path, monkeypatch) -> None:
        # Opening a directory as a text file fails on Windows and POSIX alike.
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", tmp_path)

        assert read_recent_runs() == []


# ── last_success_time ─────────────────────────────────────────────────────────


class TestLastSuccessTime:
    def test_returns_none_when_no_records_exist(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", tmp_path / "missing.jsonl")
        assert last_success_time("health_tracker.py") is None

    def test_returns_none_when_only_failures_recorded(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)
        append_run("health_tracker.py", ok=False, wrote_notion=False)

        assert last_success_time("health_tracker.py") is None

    def test_returns_latest_success_timestamp(self, tmp_path, monkeypatch) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)
        append_run("health_tracker.py", ok=True, wrote_notion=True)
        append_run("health_tracker.py", ok=False, wrote_notion=False)
        append_run("health_tracker.py", ok=True, wrote_notion=True)

        result = last_success_time("health_tracker.py")
        assert isinstance(result, datetime)

    @pytest.mark.parametrize("ok", ["false", "true", 1, 2, False, None])
    def test_only_boolean_true_is_accepted_as_success(
        self, tmp_path, monkeypatch, ok: Any
    ) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger_path.write_text(
            json.dumps({"script": "a.py", "ok": True,
                        "logged_at": "2026-10-18T12:30:00"}) + "\n"
            + json.dumps({"script": "a.py", "ok": ok,
                          "logged_at": "2030-01-01T00:00:00"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        assert last_success_time("a.py") == datetime(2026, 10, 18, 12, 30)

    @pytest.mark.parametrize("logged_at", [None, 123, [], {}, True, "", "not-a-date"])
    @pytest.mark.parametrize("with_valid_success", [False, True])
    def test_invalid_success_timestamps_are_skipped(
        self, tmp_path, monkeypatch, logged_at: Any, with_valid_success: bool
    ) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        records = [{"script": "a.py", "ok": True, "logged_at": logged_at}]
        if with_valid_success:
            records.append({"script": "a.py", "ok": True,
                            "logged_at": "2026-10-18T12:30:00"})
        ledger_path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        expected = datetime(2026, 10, 18, 12, 30) if with_valid_success else None
        assert last_success_time("a.py") == expected

    def test_returns_latest_timestamp_even_when_file_order_differs(
        self, tmp_path, monkeypatch
    ) -> None:
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger_path.write_text(
            json.dumps({"script": "a.py", "ok": True,
                        "logged_at": "2026-10-18T12:30:00"}) + "\n"
            + json.dumps({"script": "a.py", "ok": True,
                          "logged_at": "2026-10-17T12:30:00"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("utils.run_ledger._RUN_LEDGER_PATH", ledger_path)

        assert last_success_time("a.py") == datetime(2026, 10, 18, 12, 30)
