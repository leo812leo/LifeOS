"""tests/test_health_tracker.py — health_tracker 的單元測試。"""

from typing import Dict
from unittest.mock import MagicMock, patch

import pytest

from models import ActivityData
from notion_client_helper import NotionQueryError
from scripts.health_tracker import (
    HealthData,
    SleepData,
    collect_health_data,
    fetch_activities_for_date,
    fetch_body_battery,
    fetch_calories,
    fetch_resting_heart_rate,
    fetch_sleep_data,
    fetch_steps,
    fetch_stress,
    main,
    map_garmin_activity,
    sync_activities,
    write_activity_to_notion,
    write_to_notion,
)


# ── fetch_steps ────────────────────────────────────────────────────────────────


class TestFetchSteps:
    def test_sums_steps(self) -> None:
        client = MagicMock()
        client.get_steps_data.return_value = [
            {"steps": 1000},
            {"steps": 2500},
            {"steps": 3500},
        ]
        assert fetch_steps(client, "2024-01-15") == 7000

    def test_returns_none_for_empty_data(self) -> None:
        client = MagicMock()
        client.get_steps_data.return_value = []
        assert fetch_steps(client, "2024-01-15") is None

    def test_returns_none_on_exception(self) -> None:
        client = MagicMock()
        client.get_steps_data.side_effect = RuntimeError("API error")
        assert fetch_steps(client, "2024-01-15") is None


# ── fetch_resting_heart_rate ───────────────────────────────────────────────────


class TestFetchRestingHeartRate:
    def test_extracts_rhr(self) -> None:
        client = MagicMock()
        client.get_rhr_day.return_value = {
            "allMetrics": {
                "metricsMap": {
                    "WELLNESS_RESTING_HEART_RATE": [{"value": 55}]
                }
            }
        }
        assert fetch_resting_heart_rate(client, "2024-01-15") == 55

    def test_returns_none_when_missing_key(self) -> None:
        client = MagicMock()
        client.get_rhr_day.return_value = {"allMetrics": {}}
        assert fetch_resting_heart_rate(client, "2024-01-15") is None

    def test_returns_none_on_exception(self) -> None:
        client = MagicMock()
        client.get_rhr_day.side_effect = Exception("error")
        assert fetch_resting_heart_rate(client, "2024-01-15") is None


# ── fetch_sleep_data ───────────────────────────────────────────────────────────


class TestFetchSleepData:
    def test_extracts_hours_and_score(self) -> None:
        client = MagicMock()
        client.get_sleep_data.return_value = {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,  # 7.5 小時
                "sleepScores": {"overall": {"value": 82}},
            }
        }
        result = fetch_sleep_data(client, "2024-01-15")
        assert isinstance(result, SleepData)
        assert result.sleep_hours == pytest.approx(7.5)
        assert result.sleep_score == 82

    def test_returns_none_fields_on_missing_data(self) -> None:
        client = MagicMock()
        client.get_sleep_data.return_value = {"dailySleepDTO": {}}
        result = fetch_sleep_data(client, "2024-01-15")
        assert result.sleep_hours is None
        assert result.sleep_score is None

    def test_returns_none_fields_on_exception(self) -> None:
        client = MagicMock()
        client.get_sleep_data.side_effect = Exception("error")
        result = fetch_sleep_data(client, "2024-01-15")
        assert result.sleep_hours is None
        assert result.sleep_score is None


# ── fetch_stress ───────────────────────────────────────────────────────────────


class TestFetchStress:
    def test_extracts_stress(self) -> None:
        client = MagicMock()
        client.get_stress_data.return_value = {"avgStressLevel": 35}
        assert fetch_stress(client, "2024-01-15") == 35

    def test_returns_none_when_missing(self) -> None:
        client = MagicMock()
        client.get_stress_data.return_value = {}
        assert fetch_stress(client, "2024-01-15") is None

    def test_returns_none_on_exception(self) -> None:
        client = MagicMock()
        client.get_stress_data.side_effect = RuntimeError("error")
        assert fetch_stress(client, "2024-01-15") is None


# ── fetch_body_battery ─────────────────────────────────────────────────────────


class TestFetchBodyBattery:
    def test_takes_last_entry(self) -> None:
        client = MagicMock()
        client.get_body_battery.return_value = [
            {"bodyBattery": {"value": 80}},
            {"bodyBattery": {"value": 45}},
        ]
        assert fetch_body_battery(client, "2024-01-15") == 45

    def test_returns_none_for_empty_list(self) -> None:
        client = MagicMock()
        client.get_body_battery.return_value = []
        assert fetch_body_battery(client, "2024-01-15") is None

    def test_returns_none_on_exception(self) -> None:
        client = MagicMock()
        client.get_body_battery.side_effect = Exception("error")
        assert fetch_body_battery(client, "2024-01-15") is None


# ── fetch_calories ─────────────────────────────────────────────────────────────


class TestFetchCalories:
    def test_extracts_calories(self) -> None:
        client = MagicMock()
        client.get_stats.return_value = {"activeKilocalories": 450}
        assert fetch_calories(client, "2024-01-15") == 450

    def test_returns_none_when_missing(self) -> None:
        client = MagicMock()
        client.get_stats.return_value = {}
        assert fetch_calories(client, "2024-01-15") is None

    def test_returns_none_on_exception(self) -> None:
        client = MagicMock()
        client.get_stats.side_effect = RuntimeError("error")
        assert fetch_calories(client, "2024-01-15") is None


# ── collect_health_data ────────────────────────────────────────────────────────


class TestCollectHealthData:
    def test_aggregates_all_fields(self) -> None:
        client = MagicMock()

        with (
            patch("scripts.health_tracker.fetch_steps", return_value=8000),
            patch("scripts.health_tracker.fetch_resting_heart_rate", return_value=60),
            patch(
                "scripts.health_tracker.fetch_sleep_data",
                return_value=SleepData(sleep_hours=7.5, sleep_score=80),
            ),
            patch("scripts.health_tracker.fetch_stress", return_value=30),
            patch("scripts.health_tracker.fetch_body_battery", return_value=70),
            patch("scripts.health_tracker.fetch_calories", return_value=500),
        ):
            health = collect_health_data(client, "2024-01-15")

        assert health.steps == 8000
        assert health.resting_heart_rate == 60
        assert health.sleep_hours == pytest.approx(7.5)
        assert health.sleep_score == 80
        assert health.stress == 30
        assert health.body_battery == 70
        assert health.calories == 500


# ── write_to_notion ────────────────────────────────────────────────────────────


class TestWriteToNotion:
    _HEALTH = HealthData(steps=8000, resting_heart_rate=60)

    def test_skips_write_when_dedup_query_fails(self) -> None:
        """Notion 查詢失敗（NotionQueryError）時不可誤判為『沒寫過』而繼續寫入。"""
        with (
            patch(
                "scripts.health_tracker.get_notion_client", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker.page_exists_for_date",
                side_effect=NotionQueryError("boom"),
            ),
            patch("scripts.health_tracker.create_page") as mock_create,
        ):
            result = write_to_notion(self._HEALTH, "2024-01-15")

        assert result is False
        mock_create.assert_not_called()

    def test_skips_when_already_exists(self) -> None:
        with (
            patch(
                "scripts.health_tracker.get_notion_client", return_value=MagicMock()
            ),
            patch("scripts.health_tracker.page_exists_for_date", return_value=True),
            patch("scripts.health_tracker.create_page") as mock_create,
        ):
            result = write_to_notion(self._HEALTH, "2024-01-15")

        assert result is True
        mock_create.assert_not_called()

    def test_creates_page_when_not_exists(self) -> None:
        with (
            patch(
                "scripts.health_tracker.get_notion_client", return_value=MagicMock()
            ),
            patch("scripts.health_tracker.page_exists_for_date", return_value=False),
            patch(
                "scripts.health_tracker.create_page",
                return_value={"id": "page-1"},
            ) as mock_create,
        ):
            result = write_to_notion(self._HEALTH, "2024-01-15")

        assert result is True
        mock_create.assert_called_once()


# ── main（Garmin 未同步不寫佔位紀錄 + 告警）───────────────────────────────────


class TestMainKeyFieldsMissing:
    def test_no_write_alerts_and_exits_nonzero_when_all_key_fields_none(self) -> None:
        """steps/sleep/rhr 全為 None（手錶尚未同步）→ 不寫 Notion、告警、exit != 0。"""
        empty_health = HealthData()  # 所有欄位皆為 None

        with (
            patch("scripts.health_tracker.config.validate_config", return_value=True),
            patch(
                "scripts.health_tracker.garmin_login", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker.collect_health_data",
                return_value=empty_health,
            ),
            patch("scripts.health_tracker.write_to_notion") as mock_write,
            patch("scripts.health_tracker._alert_failure") as mock_alert,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code != 0
        mock_write.assert_not_called()
        mock_alert.assert_called_once()
        assert "Garmin 尚未同步" in mock_alert.call_args[0][0]

    def test_writes_when_at_least_one_key_field_present(self) -> None:
        """只要 steps/sleep/rhr 其中一項有值，就照常嘗試寫入（不視為失敗）。"""
        partial_health = HealthData(steps=8000)  # sleep/rhr 仍為 None，但 steps 有值

        with (
            patch("scripts.health_tracker.config.validate_config", return_value=True),
            patch(
                "scripts.health_tracker.garmin_login", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker.collect_health_data",
                return_value=partial_health,
            ),
            patch(
                "scripts.health_tracker.HEALTH_DB_ID", "real-db-id"
            ),
            patch(
                "scripts.health_tracker.write_to_notion", return_value=True
            ) as mock_write,
            patch("scripts.health_tracker._alert_failure") as mock_alert,
        ):
            main()

        mock_write.assert_called_once()
        mock_alert.assert_not_called()


class TestMainWriteFailureAlerts:
    def test_alerts_when_write_to_notion_fails(self) -> None:
        """write_to_notion 回傳 False → 主程式應告警。"""
        health = HealthData(steps=8000, resting_heart_rate=55)

        with (
            patch("scripts.health_tracker.config.validate_config", return_value=True),
            patch(
                "scripts.health_tracker.garmin_login", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker.collect_health_data", return_value=health
            ),
            patch("scripts.health_tracker.HEALTH_DB_ID", "real-db-id"),
            patch(
                "scripts.health_tracker.write_to_notion", return_value=False
            ),
            patch("scripts.health_tracker._alert_failure") as mock_alert,
        ):
            main()

        mock_alert.assert_called_once()
        assert "寫入失敗" in mock_alert.call_args[0][0]


# ── map_garmin_activity（T19：Garmin JSON → ActivityData 欄位映射）───────────


_RAW_RUN_ACTIVITY: Dict = {
    "activityId": 123456789,
    "activityName": "大同區 跑步",
    "activityType": {"typeKey": "running"},
    "startTimeLocal": "2024-01-15 06:30:00",
    "distance": 4800.0,  # 公尺
    "duration": 1728.0,  # 秒 → 6:00/km
    "averageHR": 152,
    "maxHR": 171,
    "calories": 320,
    "averageRunningCadenceInStepsPerMinute": 172.5,
    "avgPower": 210,
    "elevationGain": 35,
    "aerobicTrainingEffect": 3.2,
    "anaerobicTrainingEffect": 1.1,
    "vO2MaxValue": 42,
    "activityTrainingLoad": 85.0,
}


class TestMapGarminActivity:
    def test_maps_all_known_fields_for_running_activity(self) -> None:
        activity = map_garmin_activity(_RAW_RUN_ACTIVITY)

        assert activity.activity_id == 123456789
        assert activity.activity_name == "大同區 跑步"
        assert activity.activity_type == "running"
        assert activity.start_time == "2024-01-15 06:30:00"
        assert activity.distance_km == pytest.approx(4.8)
        assert activity.duration_min == pytest.approx(28.8)
        assert activity.pace == "6:00"
        assert activity.avg_hr == 152
        assert activity.max_hr == 171
        assert activity.calories == 320
        assert activity.avg_cadence == pytest.approx(172.5)
        assert activity.avg_power == 210
        assert activity.elevation_gain == 35
        assert activity.training_effect_aerobic == pytest.approx(3.2)
        assert activity.training_effect_anaerobic == pytest.approx(1.1)
        assert activity.vo2max == 42
        assert activity.training_load == pytest.approx(85.0)

    def test_non_running_activity_has_no_pace(self) -> None:
        raw = {
            "activityId": 2,
            "activityName": "重訓",
            "activityType": {"typeKey": "strength_training"},
            "distance": 0,
            "duration": 2400,
        }
        activity = map_garmin_activity(raw)
        assert activity.activity_type == "strength_training"
        assert activity.pace is None

    def test_missing_fields_map_to_none_not_guessed(self) -> None:
        """未知/缺欄位一律回傳 None，不猜測填補值。"""
        activity = map_garmin_activity({})

        assert activity.activity_id is None
        assert activity.activity_name is None
        assert activity.activity_type is None
        assert activity.distance_km is None
        assert activity.duration_min is None
        assert activity.pace is None
        assert activity.avg_hr is None
        assert activity.training_load is None


# ── write_activity_to_notion（T19：防重複 + Notion create 參數）─────────────


class TestWriteActivityToNotion:
    _ACTIVITY = ActivityData(
        activity_id=123456789,
        activity_name="大同區 跑步",
        activity_type="running",
        start_time="2024-01-15 06:30:00",
        distance_km=4.8,
        duration_min=28.8,
        pace="6:00",
        avg_hr=152,
        max_hr=171,
        calories=320,
        avg_cadence=172.5,
        avg_power=210,
        elevation_gain=35,
        training_effect_aerobic=3.2,
        training_effect_anaerobic=1.1,
        vo2max=42,
        training_load=85.0,
    )

    def test_creates_page_with_correct_properties_for_new_activity(self) -> None:
        """驗收 1：mock 一筆跑步活動 → Notion create 參數正確。"""
        with (
            patch(
                "scripts.health_tracker.get_notion_client", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker._activity_exists_in_notion",
                return_value=False,
            ),
            patch(
                "scripts.health_tracker.create_page",
                return_value={"id": "page-1"},
            ) as mock_create,
        ):
            result = write_activity_to_notion(self._ACTIVITY)

        assert result is True
        mock_create.assert_called_once()
        _, properties = mock_create.call_args[0][1:]  # (client, db_id, properties)
        assert properties["Name"] == {"title": [{"text": {"content": "大同區 跑步"}}]}
        assert properties["Activity ID"] == {"number": 123456789.0}
        assert properties["Date"] == {"date": {"start": "2024-01-15"}}
        assert properties["Type"] == {"select": {"name": "running"}}
        assert properties["Pace"] == {"rich_text": [{"text": {"content": "6:00"}}]}
        assert properties["Distance (km)"] == {"number": 4.8}
        assert properties["Duration (min)"] == {"number": 28.8}
        assert properties["Avg HR"] == {"number": 152.0}
        assert properties["Max HR"] == {"number": 171.0}
        assert properties["Calories"] == {"number": 320.0}
        assert properties["Training Load"] == {"number": 85.0}

    def test_skips_rewrite_when_activity_id_already_exists(self) -> None:
        """驗收 2：同 Activity ID 二次執行 → 跳過不重寫。"""
        with (
            patch(
                "scripts.health_tracker.get_notion_client", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker._activity_exists_in_notion",
                return_value=True,
            ),
            patch("scripts.health_tracker.create_page") as mock_create,
        ):
            result = write_activity_to_notion(self._ACTIVITY)

        assert result is None
        mock_create.assert_not_called()

    def test_returns_false_when_activity_id_missing(self) -> None:
        activity = ActivityData(activity_id=None, activity_name="無 ID 的活動")
        with patch("scripts.health_tracker.create_page") as mock_create:
            result = write_activity_to_notion(activity)

        assert result is False
        mock_create.assert_not_called()

    def test_returns_false_when_create_page_fails(self) -> None:
        with (
            patch(
                "scripts.health_tracker.get_notion_client", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker._activity_exists_in_notion",
                return_value=False,
            ),
            patch("scripts.health_tracker.create_page", return_value=None),
        ):
            result = write_activity_to_notion(self._ACTIVITY)

        assert result is False


# ── sync_activities（T19：失敗隔離 + run_ledger 雙條目）─────────────────────


class TestSyncActivities:
    def test_no_activity_day_is_not_a_failure(self) -> None:
        """跑休日（無活動列）視為正常結束，不告警、ok=True。"""
        with (
            patch(
                "scripts.health_tracker.ACTIVITY_DB_ID", "real-activity-db-id"
            ),
            patch(
                "scripts.health_tracker.fetch_activities_for_date", return_value=[]
            ),
            patch("scripts.health_tracker.append_run") as mock_append,
        ):
            sync_activities(MagicMock(), "2024-01-15")

        mock_append.assert_called_once_with(
            "health_tracker.activities", ok=True, wrote_notion=False
        )

    def test_activity_db_id_not_set_skips_without_calling_garmin(self) -> None:
        with (
            patch("scripts.health_tracker.ACTIVITY_DB_ID", ""),
            patch(
                "scripts.health_tracker.fetch_activities_for_date"
            ) as mock_fetch,
            patch("scripts.health_tracker.append_run") as mock_append,
        ):
            sync_activities(MagicMock(), "2024-01-15")

        mock_fetch.assert_not_called()
        mock_append.assert_called_once_with(
            "health_tracker.activities", ok=True, wrote_notion=False
        )

    def test_fetch_exception_isolated_marks_activities_failed(self) -> None:
        """驗收 3：mock 活動 API 拋例外 → 不往外傳播、ledger 記 activities 失敗。"""
        with (
            patch(
                "scripts.health_tracker.ACTIVITY_DB_ID", "real-activity-db-id"
            ),
            patch(
                "scripts.health_tracker.fetch_activities_for_date",
                side_effect=RuntimeError("Garmin API error"),
            ),
            patch("scripts.health_tracker.append_run") as mock_append,
        ):
            sync_activities(MagicMock(), "2024-01-15")  # 不應拋出例外

        mock_append.assert_called_once_with(
            "health_tracker.activities", ok=False, wrote_notion=False
        )

    def test_write_failure_marks_ok_false_but_does_not_raise(self) -> None:
        with (
            patch(
                "scripts.health_tracker.ACTIVITY_DB_ID", "real-activity-db-id"
            ),
            patch(
                "scripts.health_tracker.fetch_activities_for_date",
                return_value=[_RAW_RUN_ACTIVITY],
            ),
            patch(
                "scripts.health_tracker.write_activity_to_notion",
                return_value=False,
            ),
            patch("scripts.health_tracker.append_run") as mock_append,
        ):
            sync_activities(MagicMock(), "2024-01-15")

        mock_append.assert_called_once_with(
            "health_tracker.activities", ok=False, wrote_notion=False
        )

    def test_successful_write_marks_ok_true_wrote_notion_true(self) -> None:
        with (
            patch(
                "scripts.health_tracker.ACTIVITY_DB_ID", "real-activity-db-id"
            ),
            patch(
                "scripts.health_tracker.fetch_activities_for_date",
                return_value=[_RAW_RUN_ACTIVITY],
            ),
            patch(
                "scripts.health_tracker.write_activity_to_notion",
                return_value=True,
            ),
            patch("scripts.health_tracker.append_run") as mock_append,
        ):
            sync_activities(MagicMock(), "2024-01-15")

        mock_append.assert_called_once_with(
            "health_tracker.activities", ok=True, wrote_notion=True
        )

    def test_skip_existing_does_not_count_as_new_write(self) -> None:
        """dedup 跳過（None）不應誤標記為『這次有新寫入』。"""
        with (
            patch(
                "scripts.health_tracker.ACTIVITY_DB_ID", "real-activity-db-id"
            ),
            patch(
                "scripts.health_tracker.fetch_activities_for_date",
                return_value=[_RAW_RUN_ACTIVITY],
            ),
            patch(
                "scripts.health_tracker.write_activity_to_notion",
                return_value=None,
            ),
            patch("scripts.health_tracker.append_run") as mock_append,
        ):
            sync_activities(MagicMock(), "2024-01-15")

        mock_append.assert_called_once_with(
            "health_tracker.activities", ok=True, wrote_notion=False
        )


# ── fetch_activities_for_date（薄封裝，例外交由呼叫端處理）───────────────────


class TestFetchActivitiesForDate:
    def test_calls_garmin_get_activities_by_date_with_same_start_end(self) -> None:
        client = MagicMock()
        client.get_activities_by_date.return_value = [_RAW_RUN_ACTIVITY]

        result = fetch_activities_for_date(client, "2024-01-15")

        client.get_activities_by_date.assert_called_once_with(
            "2024-01-15", "2024-01-15"
        )
        assert result == [_RAW_RUN_ACTIVITY]

    def test_returns_empty_list_when_garmin_returns_none(self) -> None:
        client = MagicMock()
        client.get_activities_by_date.return_value = None

        assert fetch_activities_for_date(client, "2024-01-15") == []


# ── main（T19：健康數據與活動同步失敗隔離的端到端接線）───────────────────────


class TestMainActivitySyncIsolation:
    def test_activity_sync_exception_does_not_block_health_write(self) -> None:
        """驗收 3（端到端）：活動 API 拋例外 → 健康數據照常寫入。"""
        health = HealthData(steps=8000, resting_heart_rate=55)

        with (
            patch("scripts.health_tracker.config.validate_config", return_value=True),
            patch(
                "scripts.health_tracker.garmin_login", return_value=MagicMock()
            ),
            patch(
                "scripts.health_tracker.collect_health_data", return_value=health
            ),
            patch("scripts.health_tracker.HEALTH_DB_ID", "real-health-db-id"),
            patch(
                "scripts.health_tracker.write_to_notion", return_value=True
            ) as mock_write,
            patch("scripts.health_tracker.ACTIVITY_DB_ID", "real-activity-db-id"),
            patch(
                "scripts.health_tracker.fetch_activities_for_date",
                side_effect=RuntimeError("Garmin activities API down"),
            ),
            patch("scripts.health_tracker.append_run") as mock_append,
        ):
            main()  # 不應拋出例外

        mock_write.assert_called_once()

        calls = {c.args[0]: c.kwargs for c in mock_append.call_args_list}
        assert calls["health_tracker.py"] == {"ok": True, "wrote_notion": True}
        assert calls["health_tracker.activities"] == {
            "ok": False,
            "wrote_notion": False,
        }
