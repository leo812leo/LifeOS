"""tests/test_adherence.py — utils/adherence.py 的單元測試（T15 建議遵循率）。

聚焦驗證：
- ``read_advice_log``：日期範圍過濾、同日多行取最後一行、格式錯誤行容錯跳過。
- ``classify_day_effort``：training_load → RPE → 時長 三層 fallback 分級。
- ``compute_adherence``：rest/reduce/maintain 三種 verdict 的遵循判定矩陣，
  以及「當天無 Activity 列」排除分母的規則。
- ``format_adherence_line``：輸出格式，含「本週完全無法判定」時回傳 None。
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import ActivityData
from utils.adherence import (
    AdherenceResult,
    AdviceLogEntry,
    classify_day_effort,
    compute_adherence,
    format_adherence_line,
    read_advice_log,
)


# ── read_advice_log ──────────────────────────────────────────────────────────


class TestReadAdviceLog:
    def _write_lines(self, path: Path, payloads) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for p in payloads:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")

    def test_missing_file_returns_empty(self, tmp_path) -> None:
        result = read_advice_log(
            date(2026, 7, 6), date(2026, 7, 12), log_path=tmp_path / "nope.jsonl"
        )
        assert result == []

    def test_filters_to_week_range(self, tmp_path) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        self._write_lines(
            log_path,
            [
                {"date": "2026-07-05", "verdict": "maintain"},  # 週日之前，排除
                {"date": "2026-07-06", "verdict": "rest"},      # 週一，含
                {"date": "2026-07-12", "verdict": "reduce"},    # 週日，含
                {"date": "2026-07-13", "verdict": "maintain"},  # 下週一，排除
            ],
        )
        entries = read_advice_log(date(2026, 7, 6), date(2026, 7, 12), log_path=log_path)
        assert [e.date for e in entries] == ["2026-07-06", "2026-07-12"]

    def test_same_day_multiple_lines_keeps_last(self, tmp_path) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        self._write_lines(
            log_path,
            [
                {"date": "2026-07-06", "verdict": "maintain"},
                {"date": "2026-07-06", "verdict": "rest"},
            ],
        )
        entries = read_advice_log(date(2026, 7, 6), date(2026, 7, 12), log_path=log_path)
        assert len(entries) == 1
        assert entries[0].verdict == "rest"

    def test_skips_malformed_json_line(self, tmp_path) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write(json.dumps({"date": "2026-07-06", "verdict": "maintain"}) + "\n")
        entries = read_advice_log(date(2026, 7, 6), date(2026, 7, 12), log_path=log_path)
        assert len(entries) == 1
        assert entries[0].date == "2026-07-06"

    def test_skips_unknown_verdict(self, tmp_path) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        self._write_lines(log_path, [{"date": "2026-07-06", "verdict": "banana"}])
        entries = read_advice_log(date(2026, 7, 6), date(2026, 7, 12), log_path=log_path)
        assert entries == []

    def test_skips_missing_date_and_bad_date_format(self, tmp_path) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        self._write_lines(
            log_path,
            [
                {"verdict": "maintain"},
                {"date": "not-a-date", "verdict": "rest"},
            ],
        )
        entries = read_advice_log(date(2026, 7, 6), date(2026, 7, 12), log_path=log_path)
        assert entries == []

    def test_carries_key_metric_and_message_hash(self, tmp_path) -> None:
        log_path = tmp_path / "advice_log.jsonl"
        self._write_lines(
            log_path,
            [
                {
                    "date": "2026-07-06",
                    "verdict": "rest",
                    "key_metric": "Body Battery 18 < 30",
                    "message_hash": "abc123",
                }
            ],
        )
        entries = read_advice_log(date(2026, 7, 6), date(2026, 7, 12), log_path=log_path)
        assert entries[0].key_metric == "Body Battery 18 < 30"
        assert entries[0].message_hash == "abc123"


# ── classify_day_effort ──────────────────────────────────────────────────────


class TestClassifyDayEffort:
    def test_training_load_rest(self) -> None:
        acts = [ActivityData(training_load=10.0)]
        assert classify_day_effort(acts) == "rest"

    def test_training_load_reduce(self) -> None:
        acts = [ActivityData(training_load=60.0)]
        assert classify_day_effort(acts) == "reduce"

    def test_training_load_maintain(self) -> None:
        acts = [ActivityData(training_load=150.0)]
        assert classify_day_effort(acts) == "maintain"

    def test_training_load_sums_multiple_activities(self) -> None:
        acts = [ActivityData(training_load=20.0), ActivityData(training_load=20.0)]
        # 20 + 20 = 40 → 落在 reduce 區間（>30, <=90）
        assert classify_day_effort(acts) == "reduce"

    def test_falls_back_to_rpe_when_no_training_load(self) -> None:
        assert classify_day_effort([ActivityData(rpe=2)]) == "rest"
        assert classify_day_effort([ActivityData(rpe=5)]) == "reduce"
        assert classify_day_effort([ActivityData(rpe=9)]) == "maintain"

    def test_falls_back_to_duration_when_no_load_or_rpe(self) -> None:
        assert classify_day_effort([ActivityData(duration_min=10.0)]) == "rest"
        assert classify_day_effort([ActivityData(duration_min=30.0)]) == "reduce"
        assert classify_day_effort([ActivityData(duration_min=90.0)]) == "maintain"

    def test_no_metadata_at_all_defaults_to_rest(self) -> None:
        assert classify_day_effort([ActivityData()]) == "rest"


# ── compute_adherence ────────────────────────────────────────────────────────


class TestComputeAdherence:
    def test_rest_verdict_adherent_when_actually_rested(self) -> None:
        entries = [AdviceLogEntry(date="2026-07-06", verdict="rest")]
        activities = {"2026-07-06": [ActivityData(training_load=5.0)]}
        result = compute_adherence(entries, activities)
        assert result.judged_days == 1
        assert result.adherent_days == 1
        assert result.rest_judged == 1
        assert result.rest_adherent == 1

    def test_rest_verdict_not_adherent_when_hard_activity_logged(self) -> None:
        entries = [AdviceLogEntry(date="2026-07-06", verdict="rest")]
        activities = {"2026-07-06": [ActivityData(training_load=150.0)]}
        result = compute_adherence(entries, activities)
        assert result.judged_days == 1
        assert result.adherent_days == 0
        assert result.rest_judged == 1
        assert result.rest_adherent == 0

    def test_reduce_verdict_adherent_when_easy_or_rested(self) -> None:
        entries = [
            AdviceLogEntry(date="2026-07-06", verdict="reduce"),
            AdviceLogEntry(date="2026-07-07", verdict="reduce"),
        ]
        activities = {
            "2026-07-06": [ActivityData(training_load=60.0)],  # reduce → 遵循
            "2026-07-07": [ActivityData(training_load=5.0)],   # rest → 也算遵循（更保守）
        }
        result = compute_adherence(entries, activities)
        assert result.reduce_judged == 2
        assert result.reduce_adherent == 2

    def test_reduce_verdict_not_adherent_when_still_hard(self) -> None:
        entries = [AdviceLogEntry(date="2026-07-06", verdict="reduce")]
        activities = {"2026-07-06": [ActivityData(training_load=150.0)]}
        result = compute_adherence(entries, activities)
        assert result.reduce_judged == 1
        assert result.reduce_adherent == 0

    def test_maintain_verdict_adherent_when_reduce_or_maintain_level(self) -> None:
        entries = [
            AdviceLogEntry(date="2026-07-06", verdict="maintain"),
            AdviceLogEntry(date="2026-07-07", verdict="maintain"),
        ]
        activities = {
            "2026-07-06": [ActivityData(training_load=150.0)],  # maintain
            "2026-07-07": [ActivityData(training_load=60.0)],   # reduce → 仍算遵循
        }
        result = compute_adherence(entries, activities)
        assert result.maintain_judged == 2
        assert result.maintain_adherent == 2

    def test_maintain_verdict_not_adherent_when_actually_rested(self) -> None:
        entries = [AdviceLogEntry(date="2026-07-06", verdict="maintain")]
        activities = {"2026-07-06": [ActivityData(training_load=5.0)]}
        result = compute_adherence(entries, activities)
        assert result.maintain_judged == 1
        assert result.maintain_adherent == 0

    def test_missing_activity_day_excluded_from_denominator(self) -> None:
        entries = [
            AdviceLogEntry(date="2026-07-06", verdict="rest"),
            AdviceLogEntry(date="2026-07-07", verdict="maintain"),
        ]
        # 2026-07-07 完全沒有 Activity 列 → 排除分母，不算遵循也不算違反。
        activities = {"2026-07-06": [ActivityData(training_load=5.0)]}
        result = compute_adherence(entries, activities)
        assert result.judged_days == 1
        assert result.excluded_no_activity == 1
        assert result.adherent_days == 1

    def test_empty_activity_list_for_day_is_same_as_missing(self) -> None:
        entries = [AdviceLogEntry(date="2026-07-06", verdict="rest")]
        activities = {"2026-07-06": []}
        result = compute_adherence(entries, activities)
        assert result.judged_days == 0
        assert result.excluded_no_activity == 1

    def test_full_week_matches_brief_example_shape(self) -> None:
        # 對照任務描述範例：「本週遵循 5/7（休息建議 2/2 遵循，降載 1/2）」
        entries = [
            AdviceLogEntry(date="2026-07-06", verdict="rest"),
            AdviceLogEntry(date="2026-07-07", verdict="rest"),
            AdviceLogEntry(date="2026-07-08", verdict="reduce"),
            AdviceLogEntry(date="2026-07-09", verdict="reduce"),
            AdviceLogEntry(date="2026-07-10", verdict="maintain"),
            AdviceLogEntry(date="2026-07-11", verdict="maintain"),
            AdviceLogEntry(date="2026-07-12", verdict="maintain"),
        ]
        activities = {
            "2026-07-06": [ActivityData(training_load=5.0)],    # rest → 遵循
            "2026-07-07": [ActivityData(training_load=5.0)],    # rest → 遵循
            "2026-07-08": [ActivityData(training_load=60.0)],   # reduce → 遵循
            "2026-07-09": [ActivityData(training_load=150.0)],  # 仍硬練 → 不遵循
            "2026-07-10": [ActivityData(training_load=150.0)],  # maintain → 遵循
            "2026-07-11": [ActivityData(training_load=150.0)],  # maintain → 遵循
            "2026-07-12": [ActivityData(training_load=5.0)],    # 沒練 → 不遵循
        }
        result = compute_adherence(entries, activities)
        assert result.judged_days == 7
        assert result.adherent_days == 5
        assert (result.rest_adherent, result.rest_judged) == (2, 2)
        assert (result.reduce_adherent, result.reduce_judged) == (1, 2)
        assert (result.maintain_adherent, result.maintain_judged) == (2, 3)


# ── format_adherence_line ────────────────────────────────────────────────────


class TestFormatAdherenceLine:
    def test_returns_none_when_nothing_judged(self) -> None:
        result = AdherenceResult(
            judged_days=0,
            adherent_days=0,
            excluded_no_activity=3,
            rest_judged=0,
            rest_adherent=0,
            reduce_judged=0,
            reduce_adherent=0,
            maintain_judged=0,
            maintain_adherent=0,
        )
        assert format_adherence_line(result) is None

    def test_formats_summary_line_with_breakdown(self) -> None:
        result = AdherenceResult(
            judged_days=7,
            adherent_days=5,
            excluded_no_activity=0,
            rest_judged=2,
            rest_adherent=2,
            reduce_judged=2,
            reduce_adherent=1,
            maintain_judged=3,
            maintain_adherent=2,
        )
        line = format_adherence_line(result)
        assert "5/7" in line
        assert "休息建議 2/2" in line
        assert "降載 1/2" in line
        assert "維持 2/3" in line

    def test_mentions_excluded_days_when_present(self) -> None:
        result = AdherenceResult(
            judged_days=3,
            adherent_days=2,
            excluded_no_activity=4,
            rest_judged=3,
            rest_adherent=2,
            reduce_judged=0,
            reduce_adherent=0,
            maintain_judged=0,
            maintain_adherent=0,
        )
        line = format_adherence_line(result)
        assert "4 天無 Activity 資料排除" in line
