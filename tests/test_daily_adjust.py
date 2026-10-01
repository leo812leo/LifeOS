"""tests/test_daily_adjust.py — daily_adjust.py 的單元測試。

聚焦驗證：
- T07：daily_adjust 讀取最近 3 天 RPE/Niggle 回報，納入 AI prompt context，
  Niggle ≥3 時強制在系統提示中要求提及；``logs/advice_log.jsonl`` 寫入約定。
- T09：心跳分流（維持 + 無 warning → 一行心跳）。
- T10：規則引擎接入 — metrics → HealthData 轉換、chronic load 反推、
  判定區塊注入 prompt / 附在訊息尾、REST/EASY 的程式層覆寫防線。
- T18：睡眠一級管理 — 近 7 天 Health 紀錄讀取、睡眠債接進規則引擎、
  ``_rule_trigger_reason`` 的睡眠優先文案、就寢建議附加在完整（非心跳）訊息。

Notion / AI API 全部 mock 在 HTTP/API 邊界（比照 tests/test_ai_coach.py 的模式）。
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ActivityData, HealthData, RuleConfig, RuleVerdict
from scripts.daily_adjust import (
    _coach_week_task_is_long_run,
    _derive_chronic_load,
    _enforce_rule_verdict,
    _evaluate_rule_verdict,
    _fetch_recent_health_records,
    _fetch_recent_rpe_niggle,
    _format_rpe_niggle_section,
    _format_rule_verdict_section,
    _generate_daily_adjustment,
    _has_warning_flags,
    _infer_category_from_message,
    _is_glp1_injection_day,
    _is_long_run_today,
    _is_maintain_verdict,
    _log_advice,
    _metrics_to_health_data,
    _rule_trigger_reason,
    _tomorrow_long_run_preview_line,
    _verdict_to_category,
    _yesterday_task_summary,
    run_daily_adjust,
)
from scripts.training_advisor import GarminCoachContext, GarminCoachTask


# ── _fetch_recent_rpe_niggle ─────────────────────────────────────────────────


class TestFetchRecentRpeNiggle:
    def test_returns_empty_when_notion_not_configured(self, monkeypatch) -> None:
        monkeypatch.delenv("NOTION_API_KEY", raising=False)
        monkeypatch.delenv("ACTIVITY_DB_ID", raising=False)
        assert _fetch_recent_rpe_niggle() == []

    def test_filters_to_records_with_rpe_or_niggle(self, monkeypatch) -> None:
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")
        monkeypatch.setenv("ACTIVITY_DB_ID", "fake-db")

        records = [
            ActivityData(start_time="2026-07-11", rpe=7, notes="小腿緊"),
            ActivityData(start_time="2026-07-10", niggle_score=1),
            ActivityData(start_time="2026-07-09"),  # 無 RPE/Niggle，應被濾掉
        ]
        with patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "scripts.notion_reader.fetch_activity_history", return_value=records
        ):
            result = _fetch_recent_rpe_niggle(days=3)

        assert len(result) == 2
        assert result[0].rpe == 7
        assert result[1].niggle_score == 1

    def test_returns_empty_on_fetch_exception(self, monkeypatch) -> None:
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")
        monkeypatch.setenv("ACTIVITY_DB_ID", "fake-db")

        with patch(
            "notion_client_helper.get_notion_client",
            side_effect=RuntimeError("boom"),
        ):
            assert _fetch_recent_rpe_niggle() == []


# ── _fetch_recent_health_records（T18）───────────────────────────────────────


class TestFetchRecentHealthRecords:
    def test_returns_empty_when_notion_not_configured(self, monkeypatch) -> None:
        monkeypatch.delenv("NOTION_API_KEY", raising=False)
        monkeypatch.delenv("HEALTH_DB_ID", raising=False)
        assert _fetch_recent_health_records() == []

    def test_returns_records_from_notion_reader(self, monkeypatch) -> None:
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")
        monkeypatch.setenv("HEALTH_DB_ID", "fake-db")

        records = [HealthData(sleep_hours=5.0), HealthData(sleep_hours=7.5)]
        with patch("notion_client_helper.get_notion_client", return_value=MagicMock()), patch(
            "scripts.notion_reader.fetch_health_history", return_value=records
        ):
            result = _fetch_recent_health_records(days=7)

        assert result == records

    def test_returns_empty_on_fetch_exception(self, monkeypatch) -> None:
        monkeypatch.setenv("NOTION_API_KEY", "fake-key")
        monkeypatch.setenv("HEALTH_DB_ID", "fake-db")

        with patch(
            "notion_client_helper.get_notion_client",
            side_effect=RuntimeError("boom"),
        ):
            assert _fetch_recent_health_records() == []


# ── _format_rpe_niggle_section ────────────────────────────────────────────────


class TestFormatRpeNiggleSection:
    def test_empty_records_reports_no_data(self) -> None:
        section = _format_rpe_niggle_section([])
        assert "無" in section

    def test_formats_rpe_and_niggle_lines(self) -> None:
        records = [
            ActivityData(start_time="2026-07-11", rpe=7, notes="小腿緊"),
        ]
        section = _format_rpe_niggle_section(records)
        assert "2026-07-11" in section
        assert "RPE 7" in section
        assert "小腿緊" in section

    def test_high_niggle_adds_warning_line(self) -> None:
        records = [
            ActivityData(start_time="2026-07-10", niggle_score=4, notes="左膝"),
        ]
        section = _format_rpe_niggle_section(records)
        assert "Niggle 4" in section
        assert "⚠️" in section
        assert "必須提及" in section

    def test_low_niggle_has_no_warning(self) -> None:
        records = [ActivityData(start_time="2026-07-10", niggle_score=1)]
        section = _format_rpe_niggle_section(records)
        assert "必須提及" not in section


# ── _generate_daily_adjustment ────────────────────────────────────────────────


class TestGenerateDailyAdjustment:
    def test_includes_rpe_niggle_section_in_prompt(self) -> None:
        records = [ActivityData(start_time="2026-07-11", rpe=6)]
        with patch(
            "scripts.ai_coach._call_ai_api", return_value="測試回覆"
        ) as mock_call:
            result = _generate_daily_adjustment(
                date(2026, 7, 11), None, {}, records
            )

        assert result is not None
        user_message = mock_call.call_args.kwargs["user_message"]
        assert "RPE 6" in user_message

    def test_high_niggle_forces_mention_rule_in_system_prompt(self) -> None:
        records = [ActivityData(start_time="2026-07-11", niggle_score=4, notes="左膝")]
        with patch(
            "scripts.ai_coach._call_ai_api", return_value="測試回覆"
        ) as mock_call:
            _generate_daily_adjustment(date(2026, 7, 11), None, {}, records)

        system_prompt = mock_call.call_args.kwargs["system_prompt"]
        user_message = mock_call.call_args.kwargs["user_message"]
        assert "Niggle" in system_prompt and "必須" in system_prompt
        assert "Niggle ≥3" in user_message or "系統提醒" in user_message

    def test_no_records_defaults_to_empty_list(self) -> None:
        """rpe_niggle_records=None（呼叫端沒帶）時不應丟例外。"""
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆"):
            result = _generate_daily_adjustment(date(2026, 7, 11), None, {})
        assert result is not None


# ── 配速表與心率區間（T13）───────────────────────────────────────────────────


class TestPaceZoneWiring:
    """user_message 含 VDOT 配速表與 Karvonen 心率區間（T13）。"""

    def test_user_message_contains_pace_table_and_hr_zones(self) -> None:
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(date(2026, 7, 11), None, {})

        user_message = mock_call.call_args.kwargs["user_message"]
        assert "配速表" in user_message
        assert "心率區間" in user_message
        assert "Karvonen" in user_message

    def test_changing_vdot_env_changes_pace_table(self, monkeypatch) -> None:
        monkeypatch.delenv("VDOT", raising=False)
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(date(2026, 7, 11), None, {})
        message_default = mock_call.call_args.kwargs["user_message"]

        monkeypatch.setenv("VDOT", "42")
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(date(2026, 7, 11), None, {})
        message_42 = mock_call.call_args.kwargs["user_message"]

        assert message_default != message_42

    def test_explicit_resting_hr_changes_zone_boundaries(self) -> None:
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(date(2026, 7, 11), None, {}, resting_hr=55.0)
        message_low = mock_call.call_args.kwargs["user_message"]

        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(date(2026, 7, 11), None, {}, resting_hr=75.0)
        message_high = mock_call.call_args.kwargs["user_message"]

        assert message_low != message_high

    def test_missing_resting_hr_falls_back_to_default(self) -> None:
        from utils.vdot_paces import DEFAULT_RESTING_HR

        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(date(2026, 7, 11), None, {})

        user_message = mock_call.call_args.kwargs["user_message"]
        assert f"{DEFAULT_RESTING_HR}bpm" in user_message


# ── _log_advice ────────────────────────────────────────────────────────────────


class TestLogAdvice:
    def test_writes_jsonl_with_niggle_alert_true(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        monkeypatch.setattr("scripts.daily_adjust._ADVICE_LOG", log_path)

        records = [ActivityData(niggle_score=4)]
        _log_advice(date(2026, 7, 11), "今日建議內容", records)

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["date"] == "2026-07-11"
        assert record["niggle_alert"] is True
        assert record["message"] == "今日建議內容"

    def test_niggle_alert_false_when_no_high_niggle(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        monkeypatch.setattr("scripts.daily_adjust._ADVICE_LOG", log_path)

        _log_advice(date(2026, 7, 11), "維持原計劃", [])

        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["niggle_alert"] is False

    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch) -> None:
        # 指向一個不存在、無法建立的路徑（父目錄是檔案而非資料夾）不應讓呼叫端炸掉。
        bogus_parent = tmp_path / "not_a_dir"
        bogus_parent.write_text("x")
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", bogus_parent / "advice_log.jsonl"
        )
        _log_advice(date(2026, 7, 11), "維持原計劃", [])  # 不應拋出例外

    def test_verdict_and_key_metric_from_rule_engine(self, tmp_path, monkeypatch) -> None:
        """T15：規則引擎判定存在時，verdict/key_metric 取自結構化來源，不解析文字。"""
        log_path = tmp_path / "advice_log.jsonl"
        monkeypatch.setattr("scripts.daily_adjust._ADVICE_LOG", log_path)

        verdict = RuleVerdict(readiness_level="REST", flags=("low_body_battery",))
        _log_advice(
            date(2026, 7, 11),
            "任意訊息文字，不影響 verdict 分類",
            [],
            verdict=verdict,
            trigger_reason="Body Battery 18 < 30",
        )

        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["verdict"] == "rest"
        assert record["key_metric"] == "Body Battery 18 < 30"
        assert "message_hash" in record and len(record["message_hash"]) > 0

    def test_verdict_falls_back_to_message_parsing_when_no_rule_verdict(
        self, tmp_path, monkeypatch
    ) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        monkeypatch.setattr("scripts.daily_adjust._ADVICE_LOG", log_path)

        _log_advice(date(2026, 7, 11), "**結論**：✅ 維持", [])
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["verdict"] == "maintain"
        assert record["key_metric"] is None

    def test_message_hash_is_deterministic_for_same_message(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        monkeypatch.setattr("scripts.daily_adjust._ADVICE_LOG", log_path)

        _log_advice(date(2026, 7, 11), "相同訊息", [])
        _log_advice(date(2026, 7, 12), "相同訊息", [])

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        r1, r2 = json.loads(lines[0]), json.loads(lines[1])
        assert r1["message_hash"] == r2["message_hash"]


class TestVerdictToCategory:
    def test_rest_maps_to_rest(self) -> None:
        assert _verdict_to_category(RuleVerdict(readiness_level="REST")) == "rest"

    def test_easy_maps_to_reduce(self) -> None:
        assert _verdict_to_category(RuleVerdict(readiness_level="EASY")) == "reduce"

    def test_moderate_maps_to_maintain(self) -> None:
        assert _verdict_to_category(RuleVerdict(readiness_level="MODERATE")) == "maintain"

    def test_high_maps_to_maintain(self) -> None:
        assert _verdict_to_category(RuleVerdict(readiness_level="HIGH")) == "maintain"


class TestInferCategoryFromMessage:
    def test_maintain_conclusion(self) -> None:
        assert _infer_category_from_message("**結論**：✅ 維持") == "maintain"

    def test_rest_keyword(self) -> None:
        assert _infer_category_from_message("**結論**：⚠️ 建議調整為休息") == "rest"

    def test_other_adjustment_defaults_to_reduce(self) -> None:
        assert _infer_category_from_message("**結論**：⚠️ 調整為 4km @ 130bpm") == "reduce"


# ── run_daily_adjust（整合走線）─────────────────────────────────────────────


class TestRunDailyAdjust:
    def test_dry_run_calls_log_advice_and_returns_message(self, tmp_path, monkeypatch) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        monkeypatch.setattr("scripts.daily_adjust._ADVICE_LOG", log_path)
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._evaluate_rule_verdict", return_value=None
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment",
            return_value="☀️ 測試建議內容",
        ):
            result = run_daily_adjust(dry_run=True)

        # "☀️ 測試建議內容" 沒有「✅ 維持」標記 → 走完整建議路徑（附加睡眠區段，T18）。
        assert result.startswith("☀️ 測試建議內容")
        assert "就寢目標" in result
        assert log_path.exists()
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["message"] == result

    def test_returns_none_when_generation_fails(self) -> None:
        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment", return_value=None
        ), patch(
            "scripts.daily_adjust.append_run"
        ) as mock_append_run:
            result = run_daily_adjust(dry_run=True)

        assert result is None
        mock_append_run.assert_called_once_with(
            "daily_adjust.py", ok=False, wrote_notion=False
        )

    def test_success_path_appends_ok_run_ledger_entry(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment",
            return_value="☀️ 測試建議內容",
        ), patch(
            "scripts.daily_adjust.append_run"
        ) as mock_append_run:
            run_daily_adjust(dry_run=True)

        mock_append_run.assert_called_once_with(
            "daily_adjust.py", ok=True, wrote_notion=False
        )


# ── 心跳分流輔助函式（T09）────────────────────────────────────────────────


class TestIsGlp1InjectionDay:
    def test_matches_configured_injection_day(self, monkeypatch) -> None:
        monkeypatch.setenv("GLP1_INJECTION_DAY", "2")  # 週三
        assert _is_glp1_injection_day(date(2026, 7, 8)) is True  # 2026-07-08 是週三
        assert _is_glp1_injection_day(date(2026, 7, 9)) is False  # 週四


class TestIsMaintainVerdict:
    def test_true_when_maintain_marker_present(self) -> None:
        assert _is_maintain_verdict("**結論**：✅ 維持") is True

    def test_false_when_adjust_marker_present(self) -> None:
        assert _is_maintain_verdict("**結論**：⚠️ 建議調整為 4km @ 130bpm") is False

    def test_false_when_no_clear_marker(self) -> None:
        assert _is_maintain_verdict("今天狀態普通") is False


class TestHasWarningFlags:
    def test_injection_day_is_warning(self) -> None:
        assert _has_warning_flags(True, {}, []) is True

    def test_low_body_battery_is_warning(self) -> None:
        assert _has_warning_flags(False, {"body_battery_end": 20}, []) is True

    def test_normal_body_battery_is_not_warning(self) -> None:
        assert _has_warning_flags(False, {"body_battery_end": 60}, []) is False

    def test_high_niggle_is_warning(self) -> None:
        records = [ActivityData(niggle_score=4)]
        assert _has_warning_flags(False, {}, records) is True

    def test_no_signals_is_not_warning(self) -> None:
        assert _has_warning_flags(False, {}, []) is False

    def test_long_run_day_is_warning(self) -> None:
        """T11：長跑日必須發完整建議（附補給目標），不可壓成一行心跳。"""
        assert _has_warning_flags(False, {}, [], is_long_run_day=True) is True


# ── 長跑日補給演練（T11）──────────────────────────────────────────────


class TestIsLongRunToday:
    def test_no_coach_schedule_is_not_long_run(self) -> None:
        assert _is_long_run_today(None) is False

    def test_rest_day_is_not_long_run(self) -> None:
        coach = {"is_rest_day": True, "duration_s": 7200}
        assert _is_long_run_today(coach) is False

    def test_duration_over_90min_is_long_run(self) -> None:
        coach = {"workout_name": "Long Run", "duration_s": 105 * 60, "distance_m": None}
        assert _is_long_run_today(coach) is True

    def test_distance_fallback_when_no_duration(self) -> None:
        coach = {"workout_name": "Long Run", "duration_s": None, "distance_m": 16000}
        assert _is_long_run_today(coach) is True

    def test_short_easy_run_is_not_long_run(self) -> None:
        coach = {"workout_name": "Easy Run", "duration_s": 40 * 60, "distance_m": 6000}
        assert _is_long_run_today(coach) is False


class TestLongRunFuelLine:
    _LONG_RUN_COACH = {
        "workout_name": "Long Run",
        "description": "Z2 長跑",
        "training_effect": "AEROBIC_BASE",
        "distance_m": 18000,
        "duration_s": 120 * 60,
        "is_rest_day": False,
    }

    def _run(self, coach, ai_reply, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=coach
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._evaluate_rule_verdict", return_value=None
        ), patch(
            "scripts.daily_adjust._is_glp1_injection_day", return_value=False
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment", return_value=ai_reply
        ):
            return run_daily_adjust(dry_run=True)

    def test_long_run_day_message_contains_fuel_target_line(
        self, tmp_path, monkeypatch
    ) -> None:
        """驗收情境：長跑日早晨訊息必須附當日補給目標一行（含量化 g/hr）。"""
        result = self._run(
            self._LONG_RUN_COACH,
            "☀️ 訓練微調\n**結論**：✅ 維持",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "⛽" in result
        assert "補給演練" in result
        assert "g 碳水" in result
        assert "/fuel" in result

    def test_long_run_day_never_compresses_to_heartbeat(
        self, tmp_path, monkeypatch
    ) -> None:
        """長跑日即使 AI 說「維持」且無其他訊號，也要發完整建議 + 補給行。"""
        result = self._run(
            self._LONG_RUN_COACH,
            "☀️ 訓練微調\n**結論**：✅ 維持",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert not result.startswith("✅ 一切正常")
        assert "☀️ 訓練微調" in result

    def test_non_long_run_day_has_no_fuel_line(self, tmp_path, monkeypatch) -> None:
        short_coach = {
            "workout_name": "Easy Run",
            "distance_m": 6000,
            "duration_s": 40 * 60,
            "is_rest_day": False,
        }
        result = self._run(
            short_coach,
            "☀️ 訓練微調\n**結論**：⚠️ 建議調整為 4km @ 130bpm",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "⛽" not in result
        assert "補給演練" not in result


# ── 明日長跑前瞻 + 時態 + 四段式日報（T17）──────────────────────────────


def _coach_week_context(tasks) -> GarminCoachContext:
    """組出 ``_load_manual_coach_week`` 回傳型別的測試用 context。"""
    return GarminCoachContext(
        plan_name="手動抄錄（coach_week.json）",
        current_phase="MANUAL",
        current_phase_cn="手動抄錄課表",
        weeks_total=0,
        race_date="2026-10-25",
        tasks=tuple(tasks),
        metrics=None,
    )


def _coach_week_task(
    calendar_date: str,
    workout_name=None,
    workout_description=None,
    is_rest_day: bool = False,
) -> GarminCoachTask:
    return GarminCoachTask(
        calendar_date=calendar_date,
        workout_name=workout_name,
        workout_description=workout_description,
        training_effect=None,
        estimated_distance_m=None,
        estimated_duration_s=None,
        is_rest_day=is_rest_day,
        status=None,
    )


class TestCoachWeekTaskIsLongRun:
    def test_rest_day_is_not_long_run(self) -> None:
        task = _coach_week_task("2026-07-16", is_rest_day=True)
        assert _coach_week_task_is_long_run(task) is False

    def test_distance_in_target_text_is_long_run(self) -> None:
        task = _coach_week_task(
            "2026-07-16", workout_name="Long Run", workout_description="18km @ 7:00/km"
        )
        assert _coach_week_task_is_long_run(task) is True

    def test_short_easy_run_is_not_long_run(self) -> None:
        task = _coach_week_task(
            "2026-07-16", workout_name="Easy Run", workout_description="6km"
        )
        assert _coach_week_task_is_long_run(task) is False

    def test_name_keyword_fallback_without_numbers(self) -> None:
        """target 沒寫數字時，退而用型態關鍵字（"Long Run"）判斷。"""
        task = _coach_week_task("2026-07-16", workout_name="Long Run", workout_description=None)
        assert _coach_week_task_is_long_run(task) is True

    def test_chinese_keyword_fallback(self) -> None:
        task = _coach_week_task("2026-07-16", workout_name="長跑", workout_description=None)
        assert _coach_week_task_is_long_run(task) is True


class TestTomorrowLongRunPreviewLine:
    def test_no_coach_week_returns_none(self) -> None:
        with patch("scripts.training_advisor._load_manual_coach_week", return_value=None):
            assert _tomorrow_long_run_preview_line(date(2026, 7, 15)) is None

    def test_coach_week_read_exception_returns_none(self) -> None:
        with patch(
            "scripts.training_advisor._load_manual_coach_week",
            side_effect=RuntimeError("boom"),
        ):
            assert _tomorrow_long_run_preview_line(date(2026, 7, 15)) is None

    def test_tomorrow_not_in_tasks_returns_none(self) -> None:
        tasks = [_coach_week_task("2026-07-20", workout_name="Long Run", workout_description="18km")]
        with patch(
            "scripts.training_advisor._load_manual_coach_week",
            return_value=_coach_week_context(tasks),
        ):
            assert _tomorrow_long_run_preview_line(date(2026, 7, 15)) is None

    def test_tomorrow_short_run_returns_none(self) -> None:
        tasks = [_coach_week_task("2026-07-16", workout_name="Easy Run", workout_description="6km")]
        with patch(
            "scripts.training_advisor._load_manual_coach_week",
            return_value=_coach_week_context(tasks),
        ):
            assert _tomorrow_long_run_preview_line(date(2026, 7, 15)) is None

    def test_tomorrow_long_run_returns_quantified_line(self, monkeypatch) -> None:
        monkeypatch.setenv("RACE_DATE", "2026-10-25")
        tasks = [
            _coach_week_task("2026-07-16", workout_name="Long Run", workout_description="18km @ 7:00/km"),
        ]
        with patch(
            "scripts.training_advisor._load_manual_coach_week",
            return_value=_coach_week_context(tasks),
        ):
            line = _tomorrow_long_run_preview_line(date(2026, 7, 15))

        assert line is not None
        assert "明日長跑" in line
        assert "g" in line
        assert "\n" not in line  # 一行訊息


class TestTenseLabel:
    """①今日訓練段的時態：週間「今晚」、週末「今早」（T17 訪談重設計）。"""

    def test_weekday_header_uses_tonight_tense(self) -> None:
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆"):
            result = _generate_daily_adjustment(date(2026, 7, 15), None, {})  # 週三

        assert result is not None
        assert "今晚" in result

    def test_weekend_header_uses_morning_tense(self) -> None:
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆"):
            result = _generate_daily_adjustment(date(2026, 7, 18), None, {})  # 週六

        assert result is not None
        assert "今早" in result


class TestFourSectionDigestEndToEnd:
    """T17 驗收情境：run_daily_adjust 端到端輸出固定四段式日報（單一訊息）。"""

    def _run(self, fixed_today, coach_week_tasks, ai_reply, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())
        monkeypatch.setenv("RACE_DATE", "2026-10-25")

        class _FixedDate(date):
            @classmethod
            def today(cls):
                return fixed_today

        monkeypatch.setattr("scripts.daily_adjust.date", _FixedDate)

        yesterday = fixed_today - timedelta(days=1)
        monkeypatch.setattr(
            "scripts.daily_adjust.read_recent_runs",
            lambda *a, **k: [
                {"script": "health_tracker.py", "date": yesterday.isoformat(), "ok": True},
                {"script": "daily_adjust.py", "date": yesterday.isoformat(), "ok": True},
            ],
        )

        coach_context = (
            _coach_week_context(coach_week_tasks) if coach_week_tasks is not None else None
        )

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._evaluate_rule_verdict", return_value=None
        ), patch(
            "scripts.daily_adjust._is_glp1_injection_day", return_value=False
        ), patch(
            "scripts.training_advisor._load_manual_coach_week",
            return_value=coach_context,
        ), patch(
            "scripts.ai_coach._call_ai_api", return_value=ai_reply
        ):
            return run_daily_adjust(dry_run=True)

    def test_weekday_with_tomorrow_long_run_has_four_sections(
        self, tmp_path, monkeypatch
    ) -> None:
        """驗收情境 1：週三、明日為週四長跑 → 單一訊息含四段 + 今晚時態 + 明日碳水行。"""
        tasks = [
            _coach_week_task("2026-07-16", workout_name="Long Run", workout_description="18km @ 7:00/km"),
        ]
        result = self._run(
            date(2026, 7, 15),  # 週三
            tasks,
            "**今日排程**：節奏跑 6km\n**結論**：⚠️ 建議調整為 6km 節奏跑\n**原因**：狀況良好",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "今晚" in result  # ①今日訓練：週間時態
        assert "明日長跑" in result  # ②補給：明日碳水裝載前瞻行
        assert "就寢目標" in result  # ③睡眠
        assert "系統：" in result and "任務" in result  # ④系統心跳摘要
        assert not result.startswith("✅ 一切正常")  # 不是壓縮心跳

    def test_saturday_morning_run_uses_morning_tense(self, tmp_path, monkeypatch) -> None:
        """驗收情境 2：週六晨跑情境 → 「今早」時態。"""
        result = self._run(
            date(2026, 7, 18),  # 週六
            None,
            "**今日排程**：長跑 16km\n**結論**：⚠️ 建議調整為 16km 長跑\n**原因**：狀況良好",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "今早" in result

    def test_no_new_signal_day_stays_single_line_heartbeat(
        self, tmp_path, monkeypatch
    ) -> None:
        """驗收情境 3：無新訊號日 → 單行心跳，不因為四段式改版而退化成空殼。"""
        result = self._run(
            date(2026, 7, 15),  # 週三
            None,
            "**結論**：✅ 維持",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert result.startswith("✅ 一切正常")
        assert "\n" not in result


class TestYesterdayTaskSummary:
    def test_reports_ok_over_total_for_yesterday(self, monkeypatch) -> None:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setattr(
            "scripts.daily_adjust.read_recent_runs",
            lambda *a, **k: [
                {"script": "health_tracker.py", "date": yesterday, "ok": True},
                {"script": "investment_tracker.py", "date": yesterday, "ok": False},
                {"script": "daily_adjust.py", "date": "2020-01-01", "ok": True},  # 非昨日，應排除
            ],
        )
        assert _yesterday_task_summary() == "昨日任務 1/2 成功"

    def test_no_records_reports_no_data(self, monkeypatch) -> None:
        monkeypatch.setattr("scripts.daily_adjust.read_recent_runs", lambda *a, **k: [])
        assert _yesterday_task_summary() == "昨日任務：無紀錄"


class TestHeartbeatRouting:
    def test_maintain_and_no_warning_produces_one_line_heartbeat(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setattr(
            "scripts.daily_adjust.read_recent_runs",
            lambda *a, **k: [
                {"script": "health_tracker.py", "date": yesterday, "ok": True},
                {"script": "daily_adjust.py", "date": yesterday, "ok": True},
            ],
        )

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={"body_battery_end": 70}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._evaluate_rule_verdict", return_value=None
        ), patch(
            "scripts.daily_adjust._is_glp1_injection_day", return_value=False
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment",
            return_value="☀️ **2026-07-11 訓練微調**\n**結論**：✅ 維持",
        ):
            result = run_daily_adjust(dry_run=True)

        assert result == "✅ 一切正常 — 照手錶練。昨日任務 2/2 成功"
        assert "\n" not in result  # 心跳行不得長於一行（T18：睡眠區段不附加在心跳訊息）

    def test_maintain_but_injection_day_gives_full_advice(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())
        full_message = "☀️ **2026-07-11 訓練微調**\n**結論**：✅ 維持\n⚠️ 今天是 GLP-1 注射日"

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._evaluate_rule_verdict", return_value=None
        ), patch(
            "scripts.daily_adjust._is_glp1_injection_day", return_value=True
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment", return_value=full_message
        ):
            result = run_daily_adjust(dry_run=True)

        # T18：非心跳（完整建議）訊息一律附睡眠區段。
        assert result.startswith(full_message)
        assert "就寢目標" in result

    def test_maintain_but_high_niggle_gives_full_advice(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())
        full_message = "☀️ **2026-07-11 訓練微調**\n**結論**：✅ 維持\n⚠️ Niggle 4"
        records = [ActivityData(niggle_score=4, notes="左膝")]

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=records
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._evaluate_rule_verdict", return_value=None
        ), patch(
            "scripts.daily_adjust._is_glp1_injection_day", return_value=False
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment", return_value=full_message
        ):
            result = run_daily_adjust(dry_run=True)

        assert result.startswith(full_message)
        assert "就寢目標" in result

    def test_adjust_verdict_gives_full_advice_even_without_warnings(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())
        full_message = "☀️ **2026-07-11 訓練微調**\n**結論**：⚠️ 建議調整為 4km @ 130bpm"

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value={}
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.daily_adjust._evaluate_rule_verdict", return_value=None
        ), patch(
            "scripts.daily_adjust._is_glp1_injection_day", return_value=False
        ), patch(
            "scripts.daily_adjust._generate_daily_adjustment", return_value=full_message
        ):
            result = run_daily_adjust(dry_run=True)

        assert result.startswith(full_message)
        assert "就寢目標" in result


# ── 規則引擎接入（T10）────────────────────────────────────────────────


def _safe_glp1_day(monkeypatch) -> None:
    """把 GLP-1 注射日固定在離「今天/昨天」都遠的星期，避免規則 4
    （GLP-1 窗口）依測試執行日不同而干擾其他規則的驗證。"""
    safe_day = (date.today().weekday() + 3) % 7
    monkeypatch.setenv("GLP1_INJECTION_DAY", str(safe_day))


class TestMetricsToHealthData:
    def test_maps_all_available_fields(self) -> None:
        metrics = {
            "sleep_hours": 7.2,
            "sleep_score": 82,
            "body_battery_end": 55,
            "readiness_score": 75,
            "readiness_level": "HIGH",
            "hrv_weekly_avg": 52,
            "recovery_time_min": 120,
            "acute_load": 300,
        }
        health = _metrics_to_health_data(metrics)
        assert health.sleep_hours == 7.2
        assert health.sleep_score == 82
        assert health.body_battery == 55
        assert health.training_readiness == 75
        assert health.training_readiness_level == "HIGH"
        assert health.hrv_weekly_avg == 52
        assert health.recovery_time == 2  # 120 分鐘 → 2 小時
        assert health.acute_load == 300

    def test_empty_metrics_gives_all_none(self) -> None:
        health = _metrics_to_health_data({})
        assert health.body_battery is None
        assert health.training_readiness is None
        assert health.sleep_hours is None
        assert health.recovery_time is None

    def test_hrv_last_night_always_none(self) -> None:
        """daily_adjust 的 Garmin 路徑沒有 hrv_last_night → 引擎跳過 HRV ratio。"""
        health = _metrics_to_health_data({"hrv_weekly_avg": 52})
        assert health.hrv_last_night is None


class TestDeriveChronicLoad:
    def test_derives_from_acute_and_acwr(self) -> None:
        assert _derive_chronic_load({"acute_load": 350, "acwr_ratio": 1.4}) == pytest.approx(250.0)

    def test_missing_fields_return_none(self) -> None:
        assert _derive_chronic_load({}) is None
        assert _derive_chronic_load({"acute_load": 350}) is None
        assert _derive_chronic_load({"acwr_ratio": 1.2}) is None

    def test_zero_or_invalid_acwr_returns_none(self) -> None:
        assert _derive_chronic_load({"acute_load": 350, "acwr_ratio": 0}) is None
        assert _derive_chronic_load({"acute_load": 350, "acwr_ratio": "bad"}) is None


class TestEvaluateRuleVerdict:
    def test_low_body_battery_gives_rest(self, monkeypatch) -> None:
        _safe_glp1_day(monkeypatch)
        verdict = _evaluate_rule_verdict(date.today(), {"body_battery_end": 18})
        assert verdict is not None
        assert verdict.readiness_level == "REST"
        assert "low_body_battery" in verdict.flags

    def test_engine_error_degrades_to_none(self) -> None:
        with patch("scripts.rule_engine.evaluate", side_effect=RuntimeError("boom")):
            assert _evaluate_rule_verdict(date.today(), {}) is None


class TestRuleTriggerReason:
    def test_threshold_interpolated_from_rule_config(self) -> None:
        """閾值必須來自 RuleConfig（非硬編碼）— 換 config 說明文字要跟著變。"""
        verdict = RuleVerdict(readiness_level="REST", flags=("low_body_battery",))
        health = HealthData(body_battery=18)
        assert (
            _rule_trigger_reason(verdict, health, RuleConfig(body_battery_rest=25))
            == "Body Battery 18 < 25"
        )
        assert (
            _rule_trigger_reason(verdict, health, RuleConfig())
            == "Body Battery 18 < 30"
        )

    def test_glp1_window_reason_uses_cooldown_hours(self) -> None:
        verdict = RuleVerdict(readiness_level="EASY", flags=("glp1_window",))
        reason = _rule_trigger_reason(verdict, HealthData(), RuleConfig())
        assert "GLP-1" in reason
        assert "48" in reason


class TestFormatRuleVerdictSection:
    def test_contains_level_reason_and_warnings(self) -> None:
        verdict = RuleVerdict(
            readiness_level="REST",
            recommended_intensity="完全休息或輕度伸展",
            max_hr_zone=1,
            warnings=("過度訓練風險：ACWR 1.40 > 1.3",),
            flags=("low_body_battery",),
        )
        section = _format_rule_verdict_section(verdict, "Body Battery 18 < 30")
        assert "規則引擎判定" in section
        assert "REST" in section
        assert "Body Battery 18 < 30" in section
        assert "ACWR 1.40" in section


class TestEnforceRuleVerdict:
    _REST = RuleVerdict(
        readiness_level="REST", recommended_intensity="完全休息或輕度伸展", max_hr_zone=1
    )
    _EASY = RuleVerdict(
        readiness_level="EASY", recommended_intensity="輕鬆跑或散步", max_hr_zone=2
    )
    _MODERATE = RuleVerdict(
        readiness_level="MODERATE", recommended_intensity="輕鬆跑或輕量重訓", max_hr_zone=3
    )

    def test_rest_overrides_high_intensity_ai_output(self) -> None:
        msg = "**今日排程**：…\n**結論**：⚠️ 建議調整為 10x400m 間歇"
        out, overridden = _enforce_rule_verdict(msg, self._REST, "Body Battery 18 < 30")
        assert overridden is True
        assert "規則引擎覆寫" in out
        assert "休息" in out
        assert "間歇" not in out
        assert "Body Battery 18 < 30" in out

    def test_rest_accepts_rest_conclusion(self) -> None:
        msg = "**結論**：⚠️ 建議調整為完全休息"
        out, overridden = _enforce_rule_verdict(msg, self._REST, "reason")
        assert overridden is False
        assert out == msg

    def test_rest_overrides_maintain_without_rest_keyword(self) -> None:
        msg = "**今日排程**：輕鬆跑 6km\n**結論**：✅ 維持"
        _, overridden = _enforce_rule_verdict(msg, self._REST, "reason")
        assert overridden is True

    def test_easy_allows_maintain(self) -> None:
        msg = "**結論**：✅ 維持"
        out, overridden = _enforce_rule_verdict(msg, self._EASY, "reason")
        assert overridden is False
        assert out == msg

    def test_easy_blocks_tempo(self) -> None:
        msg = "**結論**：⚠️ 建議調整為 Tempo 5km"
        _, overridden = _enforce_rule_verdict(msg, self._EASY, "reason")
        assert overridden is True

    def test_moderate_never_overrides(self) -> None:
        msg = "**結論**：⚠️ 建議調整為 10x400m 間歇"
        out, overridden = _enforce_rule_verdict(msg, self._MODERATE, "reason")
        assert overridden is False
        assert out == msg

    def test_schedule_mention_does_not_false_positive(self) -> None:
        """「今日排程」段落引用 Garmin 原排程（節奏跑），結論已改休息 → 不覆寫。"""
        msg = "**今日排程**：節奏跑 8km\n**結論**：⚠️ 建議調整為完全休息"
        _, overridden = _enforce_rule_verdict(msg, self._REST, "reason")
        assert overridden is False


class TestGenerateWithRuleVerdict:
    _SECTION = (
        "🧭 規則引擎判定（確定性規則，非 AI）：\n"
        "- 等級：REST — 完全休息或輕度伸展（最高心率區間 Z1）\n"
        "- 依據：Body Battery 18 < 30"
    )

    def test_rule_section_injected_into_user_message(self) -> None:
        verdict = RuleVerdict(
            readiness_level="REST", recommended_intensity="完全休息或輕度伸展", max_hr_zone=1
        )
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(
                date(2026, 7, 11), None, {}, [], verdict=verdict, rule_section=self._SECTION
            )

        user_message = mock_call.call_args.kwargs["user_message"]
        assert "規則引擎判定" in user_message
        assert "Body Battery 18 < 30" in user_message
        system_prompt = mock_call.call_args.kwargs["system_prompt"]
        assert "規則引擎硬性上限" in system_prompt

    def test_no_hard_constraint_for_moderate_verdict(self) -> None:
        verdict = RuleVerdict(
            readiness_level="MODERATE", recommended_intensity="輕鬆跑或輕量重訓", max_hr_zone=3
        )
        with patch("scripts.ai_coach._call_ai_api", return_value="測試回覆") as mock_call:
            _generate_daily_adjustment(
                date(2026, 7, 11), None, {}, [], verdict=verdict, rule_section="🧭 規則引擎判定：MODERATE"
            )

        assert "規則引擎硬性上限" not in mock_call.call_args.kwargs["system_prompt"]
        assert "規則引擎判定" in mock_call.call_args.kwargs["user_message"]


class TestRuleEngineEndToEnd:
    """run_daily_adjust 整合走線：真實 rule_engine + mock AI（HTTP 邊界）。"""

    def _run(self, metrics: dict, ai_reply: str, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())
        _safe_glp1_day(monkeypatch)

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value=metrics
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records", return_value=[]
        ), patch(
            "scripts.ai_coach._call_ai_api", return_value=ai_reply
        ):
            return run_daily_adjust(dry_run=True)

    def test_body_battery_18_forces_rest_even_if_ai_says_high_intensity(
        self, tmp_path, monkeypatch
    ) -> None:
        """驗收情境：Body Battery=18 → 即使 mock AI 回覆高強度，輸出必須是休息建議。"""
        result = self._run(
            {"body_battery_end": 18},
            "**結論**：⚠️ 建議調整為 10x400m 間歇",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "規則引擎覆寫" in result
        assert "休息" in result
        assert "間歇" not in result
        assert "Body Battery 18 < 30" in result  # 閾值由 RuleConfig 內插

    def test_dry_run_output_contains_rule_verdict_section(
        self, tmp_path, monkeypatch
    ) -> None:
        """驗收情境：--dry-run 輸出（回傳訊息）必須含規則引擎判定區塊。"""
        result = self._run(
            {"readiness_score": 75, "sleep_hours": 7.2},
            "**結論**：⚠️ 建議調整為 4km @ 130bpm",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "🧭 規則引擎判定" in result
        assert "HIGH" in result  # Readiness 75 + 無 HRV ratio → Rule 7 HIGH

    def test_rest_verdict_blocks_heartbeat_even_on_maintain(
        self, tmp_path, monkeypatch
    ) -> None:
        """Readiness 35 → REST（規則 2），即使 AI 說維持且無 warning 旗標，
        也不得壓縮成一行心跳，且會被覆寫為休息建議。"""
        result = self._run(
            {"readiness_score": 35},
            "**結論**：✅ 維持",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert not result.startswith("✅ 一切正常")
        assert "規則引擎覆寫" in result
        assert "休息" in result


# ── 睡眠一級管理端到端（T18）─────────────────────────────────────────────


class TestSleepDebtEndToEnd:
    """run_daily_adjust 整合走線：真實 rule_engine + 真實 sleep_debt_7d（T18）。"""

    def _run(self, health_records, metrics, ai_reply, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.daily_adjust._ADVICE_LOG", tmp_path / "advice_log.jsonl"
        )
        monkeypatch.setattr("scripts.daily_adjust.append_run", MagicMock())
        _safe_glp1_day(monkeypatch)

        with patch(
            "scripts.daily_adjust._fetch_today_garmin_coach", return_value=None
        ), patch(
            "scripts.daily_adjust._fetch_latest_metrics", return_value=metrics
        ), patch(
            "scripts.daily_adjust._fetch_recent_rpe_niggle", return_value=[]
        ), patch(
            "scripts.daily_adjust._fetch_recent_health_records",
            return_value=health_records,
        ), patch(
            "scripts.ai_coach._call_ai_api", return_value=ai_reply
        ):
            return run_daily_adjust(dry_run=True)

    def test_seven_days_at_5h_triggers_sleep_priority_and_bedtime_target(
        self, tmp_path, monkeypatch
    ) -> None:
        """驗收情境 1：連續 7 天 5h 睡眠 → 債務 14h、規則觸發、日報含
        「睡眠優先」與就寢目標。"""
        records = [HealthData(sleep_hours=5.0, sleep_score=55) for _ in range(7)]
        result = self._run(
            records,
            {"readiness_score": 75},  # 昨晚睡眠來自 health_records，不經 metrics
            "**結論**：✅ 維持",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "睡眠優先" in result
        assert "14.0h" in result
        assert "就寢目標" in result
        # 睡眠規則觸發 → EASY，不允許壓縮成心跳。
        assert not result.startswith("✅ 一切正常")

    def test_seven_days_at_7_5h_does_not_trigger_but_shows_bedtime(
        self, tmp_path, monkeypatch
    ) -> None:
        """驗收情境 2：連續 7 天 7.5h → 不觸發，就寢行仍顯示（管理是常態，
        不是懲罰）——維持/正常路徑仍會走完整訊息（因未 mock heartbeat 條件）。"""
        records = [HealthData(sleep_hours=7.5, sleep_score=88) for _ in range(7)]
        result = self._run(
            records,
            {"readiness_score": 75},
            "**結論**：⚠️ 建議調整為 4km @ 130bpm",
            tmp_path,
            monkeypatch,
        )

        assert result is not None
        assert "睡眠優先" not in result
        assert "就寢目標" in result
