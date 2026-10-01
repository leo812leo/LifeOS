"""tests/test_training_advisor.py — training_advisor（delta-advisor）的單元測試。"""

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from models import ActivityData, AthleteProfile, HealthData, WeeklyContext
from scripts.training_advisor import (
    GarminCoachContext,
    _build_fallback_plan,
    _format_context_for_prompt,
    _format_garmin_coach_for_prompt,
    _format_no_coach_advisory,
    _generate_weekly_plan,
    _load_athlete_profile,
    _load_manual_coach_week,
    run_weekly_advisor,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_profile() -> AthleteProfile:
    return AthleteProfile(
        weight_kg=70.0,
        target_weight_kg=65.0,
        vdot=38.0,
        max_hr=185,
        glp1_injection_day=4,  # 週五
        easy_pace_min=6.5,
        easy_pace_max=7.0,
        run_days=(1, 3, 5),
        strength_days=(0, 2),
        protein_target_g=160,
        water_target_l=3.0,
    )


@pytest.fixture
def empty_context() -> WeeklyContext:
    return WeeklyContext(
        days=7,
        health_records=(),
        activity_records=(),
        total_distance_km=0.0,
        total_training_load=0.0,
        run_count=0,
        strength_count=0,
        avg_sleep_score=None,
        avg_hrv=None,
        avg_resting_hr=None,
        chronic_load=None,
    )


@pytest.fixture
def rich_context() -> WeeklyContext:
    health = HealthData(
        steps=9000,
        resting_heart_rate=56,
        sleep_hours=7.5,
        sleep_score=80,
        stress=28,
        body_battery=70,
        calories=450,
        training_readiness=72,
        hrv_last_night=55,
        hrv_weekly_avg=53,
    )
    activity = ActivityData(
        activity_type="running",
        distance_km=10.0,
        duration_min=65.0,
        pace="6:30",
        avg_hr=145,
        training_load=85.0,
    )
    return WeeklyContext(
        days=7,
        health_records=(health,),
        activity_records=(activity,),
        total_distance_km=32.0,
        total_training_load=240.0,
        run_count=3,
        strength_count=2,
        avg_sleep_score=80.0,
        avg_hrv=53.5,
        avg_resting_hr=57.0,
        chronic_load=230.0,
    )


@pytest.fixture
def valid_coach_week() -> List[dict]:
    """coach_week.json 的合法內容（7 筆 {day, type, target}）。"""
    return [
        {"day": "2026-07-13", "type": "Easy Run", "target": "8km @ 6:50/km"},
        {"day": "2026-07-14", "type": "Rest"},
        {"day": "2026-07-15", "type": "Interval", "target": "6x800m @ 5:10/km"},
        {"day": "2026-07-16", "type": "Easy Run", "target": "6km"},
        {"day": "2026-07-17", "type": "休息", "target": None},
        {"day": "2026-07-18", "type": "Long Run", "target": "18km @ 7:00/km"},
        {"day": "2026-07-19", "type": "Recovery", "target": "5km"},
    ]


# ── TestLoadAthleteProfile ─────────────────────────────────────────────────────


class TestLoadAthleteProfile:
    def test_returns_athlete_profile(self) -> None:
        profile = _load_athlete_profile()
        assert isinstance(profile, AthleteProfile)
        assert profile.weight_kg == 70.0
        assert profile.vdot == 38.0
        assert profile.max_hr == 185

    def test_default_injection_day_is_friday(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            # 確保不受環境變數影響，使用預設值 4（週五）
            import os
            original = os.environ.pop("GLP1_INJECTION_DAY", None)
            try:
                profile = _load_athlete_profile()
                assert profile.glp1_injection_day == 4
            finally:
                if original is not None:
                    os.environ["GLP1_INJECTION_DAY"] = original

    def test_reads_injection_day_from_env(self) -> None:
        with patch.dict("os.environ", {"GLP1_INJECTION_DAY": "2"}):
            profile = _load_athlete_profile()
            assert profile.glp1_injection_day == 2

    def test_profile_is_immutable(self) -> None:
        from dataclasses import FrozenInstanceError
        profile = _load_athlete_profile()
        with pytest.raises(FrozenInstanceError):
            profile.weight_kg = 80.0  # type: ignore[misc]

    def test_protein_and_water_targets(self) -> None:
        profile = _load_athlete_profile()
        assert profile.protein_target_g == 160
        assert profile.water_target_l == 3.0

    def test_run_and_strength_days(self) -> None:
        profile = _load_athlete_profile()
        assert 1 in profile.run_days     # 週二
        assert 3 in profile.run_days     # 週四
        assert 5 in profile.run_days     # 週六
        assert 0 in profile.strength_days  # 週一
        assert 2 in profile.strength_days  # 週三


# ── TestFormatContextForPrompt ────────────────────────────────────────────────


class TestFormatContextForPrompt:
    def test_empty_context_produces_string(self, empty_context: WeeklyContext) -> None:
        result = _format_context_for_prompt(empty_context)
        assert isinstance(result, str)
        assert "0 次" in result  # 跑步 0 次

    def test_rich_context_includes_all_fields(self, rich_context: WeeklyContext) -> None:
        result = _format_context_for_prompt(rich_context)
        assert "32.0 km" in result
        assert "3 次" in result   # 跑步次數
        assert "240.0" in result  # 訓練負荷

    def test_activity_records_appended(self, rich_context: WeeklyContext) -> None:
        result = _format_context_for_prompt(rich_context)
        assert "running" in result
        assert "10.0km" in result


# ── T12: 手動課表備援（coach_week.json）──────────────────────────────────────


def _write_coach_week(tmp_path: Path, data: object, age_days: float = 0.0) -> Path:
    """在 tmp_path 寫一份 coach_week.json，可選擇性把 mtime 調舊。"""
    path = tmp_path / "coach_week.json"
    content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.write_text(content, encoding="utf-8")
    if age_days > 0:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


class TestLoadManualCoachWeek:
    def test_valid_file_returns_context(self, tmp_path: Path, valid_coach_week: List[dict]) -> None:
        path = _write_coach_week(tmp_path, valid_coach_week)
        ctx = _load_manual_coach_week(paths=[path])
        assert ctx is not None
        assert len(ctx.tasks) == 7
        assert ctx.current_phase == "MANUAL"
        assert "coach_week.json" in ctx.plan_name

    def test_rest_day_detection(self, tmp_path: Path, valid_coach_week: List[dict]) -> None:
        path = _write_coach_week(tmp_path, valid_coach_week)
        ctx = _load_manual_coach_week(paths=[path])
        assert ctx is not None
        # "Rest"（英文）與「休息」（中文）都要判定為休息日
        assert ctx.tasks[1].is_rest_day is True
        assert ctx.tasks[1].workout_name is None
        assert ctx.tasks[4].is_rest_day is True
        # 訓練日不是休息日，type/target 對應 workout_name/description
        assert ctx.tasks[0].is_rest_day is False
        assert ctx.tasks[0].workout_name == "Easy Run"
        assert ctx.tasks[0].workout_description == "8km @ 6:50/km"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _load_manual_coach_week(paths=[tmp_path / "coach_week.json"]) is None

    def test_stale_file_returns_none(self, tmp_path: Path, valid_coach_week: List[dict]) -> None:
        """過期（mtime > 7 天）視為無課表。"""
        path = _write_coach_week(tmp_path, valid_coach_week, age_days=8)
        assert _load_manual_coach_week(paths=[path]) is None

    def test_fresh_file_within_seven_days_accepted(
        self, tmp_path: Path, valid_coach_week: List[dict]
    ) -> None:
        path = _write_coach_week(tmp_path, valid_coach_week, age_days=6)
        assert _load_manual_coach_week(paths=[path]) is not None

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        path = _write_coach_week(tmp_path, "{這不是 JSON")
        assert _load_manual_coach_week(paths=[path]) is None

    def test_non_list_returns_none(self, tmp_path: Path) -> None:
        path = _write_coach_week(tmp_path, {"day": "2026-07-13", "type": "Easy"})
        assert _load_manual_coach_week(paths=[path]) is None

    def test_entry_missing_type_returns_none(self, tmp_path: Path) -> None:
        path = _write_coach_week(tmp_path, [{"day": "2026-07-13"}])
        assert _load_manual_coach_week(paths=[path]) is None

    def test_first_existing_path_wins(self, tmp_path: Path, valid_coach_week: List[dict]) -> None:
        primary_dir = tmp_path / "root"
        primary_dir.mkdir()
        primary = _write_coach_week(primary_dir, valid_coach_week)
        missing = tmp_path / "logs" / "coach_week.json"
        ctx = _load_manual_coach_week(paths=[missing, primary])
        assert ctx is not None

    def test_non_seven_entries_still_accepted(self, tmp_path: Path) -> None:
        """筆數不是 7 只警告不拒收（使用者可能漏抄）。"""
        path = _write_coach_week(tmp_path, [
            {"day": "2026-07-13", "type": "Easy Run", "target": "8km"},
            {"day": "2026-07-14", "type": "Rest"},
        ])
        ctx = _load_manual_coach_week(paths=[path])
        assert ctx is not None
        assert len(ctx.tasks) == 2

    def test_manual_context_formats_non_iso_day_labels(self, tmp_path: Path) -> None:
        """day 用「週一」這類標籤時，prompt 格式化不加空括號。"""
        path = _write_coach_week(tmp_path, [
            {"day": "週一", "type": "Easy Run", "target": "8km"},
            {"day": "週二", "type": "Rest"},
        ])
        ctx = _load_manual_coach_week(paths=[path])
        assert ctx is not None
        rendered = _format_garmin_coach_for_prompt(ctx)
        assert "- 週一：Easy Run" in rendered
        assert "（）" not in rendered


# ── T12: 兩種模式的 prompt 與輸出 ─────────────────────────────────────────────


class TestCoachDeltaMode:
    def test_system_prompt_demands_delta_not_new_plan(
        self, rich_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        coach = GarminCoachContext(
            plan_name="Garmin Coach 馬拉松",
            current_phase="BUILD",
            current_phase_cn="建量期",
            weeks_total=16,
            race_date="2026-10-25",
            tasks=(),
        )
        with patch("scripts.ai_coach._call_ai_api", return_value="修正建議內容") as mock_call:
            result = _generate_weekly_plan(rich_context, sample_profile, coach=coach)

        system_prompt = mock_call.call_args.kwargs["system_prompt"]
        assert "不要生成新課表" in system_prompt
        assert "修正清單（delta）" in system_prompt
        assert "優先重排" in system_prompt  # 關鍵課重排而非刪除
        assert result is not None
        assert "週訓練修正建議" in result
        assert "修正建議內容" in result


class TestPaceZoneWiring:
    """coach-mode user_message 含 VDOT 配速表與 Karvonen 心率區間（T13）。"""

    def _coach(self) -> GarminCoachContext:
        return GarminCoachContext(
            plan_name="Garmin Coach 馬拉松",
            current_phase="BUILD",
            current_phase_cn="建量期",
            weeks_total=16,
            race_date="2026-10-25",
            tasks=(),
        )

    def test_user_message_contains_pace_table_and_hr_zones(
        self, rich_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        with patch("scripts.ai_coach._call_ai_api", return_value="修正建議") as mock_call:
            _generate_weekly_plan(rich_context, sample_profile, coach=self._coach())

        user_message = mock_call.call_args.kwargs["user_message"]
        assert "配速表" in user_message
        assert "心率區間" in user_message
        assert "Karvonen" in user_message

    def test_changing_vdot_changes_pace_table_in_prompt(
        self, rich_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        from dataclasses import replace

        profile_42 = replace(sample_profile, vdot=42.0)

        with patch("scripts.ai_coach._call_ai_api", return_value="修正建議") as mock_call:
            _generate_weekly_plan(rich_context, sample_profile, coach=self._coach())
        message_38 = mock_call.call_args.kwargs["user_message"]

        with patch("scripts.ai_coach._call_ai_api", return_value="修正建議") as mock_call:
            _generate_weekly_plan(rich_context, profile_42, coach=self._coach())
        message_42 = mock_call.call_args.kwargs["user_message"]

        assert message_38 != message_42

    def test_uses_context_avg_resting_hr_for_zones(
        self, sample_profile: AthleteProfile
    ) -> None:
        """WeeklyContext.avg_resting_hr 存在時，心率區間要用實測值而非預設值。"""
        from dataclasses import replace

        health = HealthData(resting_heart_rate=70)
        context = WeeklyContext(
            days=7,
            health_records=(health,),
            activity_records=(),
            total_distance_km=0.0,
            total_training_load=0.0,
            run_count=0,
            strength_count=0,
            avg_sleep_score=None,
            avg_hrv=None,
            avg_resting_hr=70.0,
            chronic_load=None,
        )

        with patch("scripts.ai_coach._call_ai_api", return_value="修正建議") as mock_call:
            _generate_weekly_plan(context, sample_profile, coach=self._coach())

        user_message = mock_call.call_args.kwargs["user_message"]
        assert "70" in user_message

    def test_missing_avg_resting_hr_falls_back_to_default(
        self, empty_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        """WeeklyContext.avg_resting_hr 為 None 時退回預設安靜心率。"""
        from utils.vdot_paces import DEFAULT_RESTING_HR

        with patch("scripts.ai_coach._call_ai_api", return_value="修正建議") as mock_call:
            _generate_weekly_plan(empty_context, sample_profile, coach=self._coach())

        user_message = mock_call.call_args.kwargs["user_message"]
        assert f"{DEFAULT_RESTING_HR}bpm" in user_message


class TestNoCoachAdvisoryMode:
    def test_prompt_declares_no_schedule_and_forbids_plan(
        self, rich_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        with patch("scripts.ai_coach._call_ai_api", return_value="評估內容") as mock_call:
            _generate_weekly_plan(rich_context, sample_profile, coach=None)

        system_prompt = mock_call.call_args.kwargs["system_prompt"]
        assert "絕不編造" in system_prompt
        user_message = mock_call.call_args.kwargs["user_message"]
        assert "不要編造課表" in user_message
        assert "未讀到 Garmin Coach 課表" in user_message
        # 不再要求 7 天 JSON 課表
        assert "session_type" not in user_message
        assert "JSON 為包含 7 個元素" not in user_message

    def test_output_wrapped_with_no_coach_declaration(
        self, rich_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        """無課表 → 輸出開頭必為「未讀到課表」聲明（不依賴 AI 自律）。"""
        with patch("scripts.ai_coach._call_ai_api", return_value="評估內容"):
            result = _generate_weekly_plan(rich_context, sample_profile, coach=None)

        assert result is not None
        assert "未讀到 Garmin Coach 課表" in result
        assert "不含訓練課表" in result
        assert "評估內容" in result

    def test_format_no_coach_advisory_prepends_header(self) -> None:
        result = _format_no_coach_advisory("AI 回應本體")
        assert result.startswith("🩺")
        assert "未讀到 Garmin Coach 課表" in result
        assert result.endswith("AI 回應本體")


class TestManualCoachWeekWiring:
    """run_weekly_advisor 的課表來源鏈：API → coach_week.json → 純諮詢。"""

    @staticmethod
    def _run_with_sources(api_ctx, manual_ctx):
        mock_context = WeeklyContext(
            days=7,
            health_records=(),
            activity_records=(),
            total_distance_km=20.0,
            total_training_load=150.0,
            run_count=2,
            strength_count=1,
        )
        with (
            patch.dict("os.environ", {
                "NOTION_API_KEY": "test-key",
                "HEALTH_DB_ID": "db-health",
                "ACTIVITY_DB_ID": "",
                "ANTHROPIC_API_KEY": "",
            }),
            patch("scripts.training_advisor.get_notion_client", return_value=MagicMock()),
            patch("scripts.training_advisor.fetch_health_history", return_value=[]),
            patch("scripts.training_advisor.build_weekly_context", return_value=mock_context),
            patch("scripts.training_advisor._fetch_garmin_coach_context", return_value=api_ctx),
            patch("scripts.training_advisor._load_manual_coach_week", return_value=manual_ctx),
            patch("scripts.training_advisor._generate_weekly_plan", return_value="PLAN") as mock_gen,
        ):
            result = run_weekly_advisor(dry_run=True)
        return result, mock_gen

    def test_manual_file_used_when_api_unavailable(self) -> None:
        manual_ctx = GarminCoachContext(
            plan_name="手動抄錄（coach_week.json）",
            current_phase="MANUAL",
            current_phase_cn="手動抄錄課表",
            weeks_total=0,
            race_date="2026-10-25",
            tasks=(),
        )
        result, mock_gen = self._run_with_sources(api_ctx=None, manual_ctx=manual_ctx)
        assert result == "PLAN"
        # _generate_weekly_plan 收到手動課表 context
        assert mock_gen.call_args.args[2] is manual_ctx

    def test_advisory_mode_when_api_and_manual_unavailable(self) -> None:
        result, mock_gen = self._run_with_sources(api_ctx=None, manual_ctx=None)
        assert result == "PLAN"
        assert mock_gen.call_args.args[2] is None


# ── TestBuildFallbackPlan ────────────────────────────────────────────────────


class TestBuildFallbackPlan:
    def test_returns_string(self, empty_context: WeeklyContext, sample_profile: AthleteProfile) -> None:
        result = _build_fallback_plan(empty_context, sample_profile)
        assert isinstance(result, str)
        assert len(result) > 50

    def test_includes_fallback_warning(self, empty_context: WeeklyContext, sample_profile: AthleteProfile) -> None:
        result = _build_fallback_plan(empty_context, sample_profile)
        assert "API" in result or "fallback" in result.lower() or "Fallback" in result

    def test_no_coach_fallback_contains_no_schedule(
        self, empty_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        """T12：無 Coach 課表時，fallback 絕不生成逐日課表，只給聲明 + 身體狀態。"""
        result = _build_fallback_plan(empty_context, sample_profile)
        assert "無法取得 Garmin Coach 課表" in result
        assert "不含訓練課表" in result
        assert "身體狀態摘要" in result
        # 不得出現舊版 rule-based 的逐日排課
        assert "週一：" not in result
        assert "節奏跑" not in result
        assert "LSD" not in result

    def test_coach_fallback_relays_schedule_verbatim(
        self, empty_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        """有 Coach 課表時，fallback 轉述 Coach 排程（可保留原樣）。"""
        from scripts.training_advisor import GarminCoachTask

        coach = GarminCoachContext(
            plan_name="Garmin Coach 馬拉松",
            current_phase="BUILD",
            current_phase_cn="建量期",
            weeks_total=16,
            race_date="2026-10-25",
            tasks=(
                GarminCoachTask(
                    calendar_date="2026-07-13",
                    workout_name="Easy Run",
                    workout_description="8km",
                    training_effect=None,
                    estimated_distance_m=8000.0,
                    estimated_duration_s=None,
                    is_rest_day=False,
                    status=None,
                ),
            ),
        )
        result = _build_fallback_plan(empty_context, sample_profile, coach=coach)
        assert "本週 Garmin Coach 排程" in result
        assert "Easy Run" in result

    def test_includes_injection_day(self, empty_context: WeeklyContext, sample_profile: AthleteProfile) -> None:
        result = _build_fallback_plan(empty_context, sample_profile)
        assert "GLP-1" in result

    def test_includes_race_countdown(self, empty_context: WeeklyContext, sample_profile: AthleteProfile) -> None:
        result = _build_fallback_plan(empty_context, sample_profile)
        assert "目標賽事" in result

    def test_phase_changes_based_on_weeks_to_race(self, empty_context: WeeklyContext, sample_profile: AthleteProfile) -> None:
        """距賽事週數不同時，訓練期別（減量期/建量期/基礎期）應正確切換。"""
        from datetime import timedelta
        from unittest.mock import patch as mock_patch

        # 模擬距離賽事僅剩 3 週 → 減量期
        near_race_date = date.today() + timedelta(weeks=3)
        with mock_patch("scripts.training_advisor._get_race_date", return_value=near_race_date):
            result_near = _build_fallback_plan(empty_context, sample_profile)

        # 模擬距離賽事還有 12 週 → 建量期（8 < 12 <= 16）
        mid_race_date = date.today() + timedelta(weeks=12)
        with mock_patch("scripts.training_advisor._get_race_date", return_value=mid_race_date):
            result_mid = _build_fallback_plan(empty_context, sample_profile)

        # 模擬距離賽事還有 20 週 → 基礎期（> 16）
        far_race_date = date.today() + timedelta(weeks=20)
        with mock_patch("scripts.training_advisor._get_race_date", return_value=far_race_date):
            result_far = _build_fallback_plan(empty_context, sample_profile)

        assert "減量期" in result_near
        assert "建量期" in result_mid
        assert "基礎期" in result_far


# ── T11: 長跑補給與腸胃訓練協議 ───────────────────────────────────────────────


@pytest.fixture
def fueled_context() -> WeeklyContext:
    """含最近長跑 /fuel 回報（碳水 45g / GI 2）的訓練上下文。"""
    activity = ActivityData(
        activity_type="running",
        start_time="2026-07-08T06:30:00.000+08:00",
        distance_km=18.0,
        duration_min=120.0,
        fuel_carbs_g=45.0,
        gi_score=2,
        fuel_plan="兩包gel+500ml電解質",
    )
    return WeeklyContext(
        days=7,
        health_records=(),
        activity_records=(activity,),
        total_distance_km=30.0,
        total_training_load=210.0,
        run_count=3,
        strength_count=2,
    )


class TestFuelingInPrompts:
    def test_template_renders_with_fuel_placeholders(
        self, fueled_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        """prompt 模板含新佔位符（fuel_target/fuel_history）且渲染不炸（無 coach 路徑）。"""
        from datetime import timedelta

        race = date.today() + timedelta(weeks=15)  # ≥13 週 → 30-40g/hr
        with patch(
            "scripts.training_advisor._get_race_date", return_value=race
        ), patch(
            "scripts.ai_coach._call_ai_api", return_value="[]"
        ) as mock_call:
            _generate_weekly_plan(fueled_context, sample_profile, coach=None)

        user_message = mock_call.call_args.kwargs["user_message"]
        assert "賽中補給與腸胃訓練" in user_message
        assert "每小時 30-40g 碳水" in user_message
        # 最近 /fuel 回報要被餵進 prompt（AI 依 GI 調整下一次目標）
        assert "碳水 45g" in user_message
        assert "GI 2" in user_message
        # GI 調整規則
        assert "GI Score ≥3" in user_message

    def test_coach_mode_injects_fueling_rules_and_history(
        self, fueled_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        """Garmin Coach 模式：系統提示含補給協議，使用者訊息含目標 + 回報。"""
        from datetime import timedelta

        coach = GarminCoachContext(
            plan_name="Garmin Coach 馬拉松",
            current_phase="BUILD",
            current_phase_cn="建量期",
            weeks_total=16,
            race_date="2026-10-25",
            tasks=(),
        )
        race = date.today() + timedelta(weeks=10)  # 9-12 週 → 40-60g/hr
        with patch(
            "scripts.training_advisor._get_race_date", return_value=race
        ), patch(
            "scripts.ai_coach._call_ai_api", return_value="測試修正建議"
        ) as mock_call:
            result = _generate_weekly_plan(fueled_context, sample_profile, coach=coach)

        assert result is not None
        system_prompt = mock_call.call_args.kwargs["system_prompt"]
        assert "腸胃訓練" in system_prompt
        assert "60-90g" in system_prompt  # 賽事目標
        assert "GI ≥3" in system_prompt

        user_message = mock_call.call_args.kwargs["user_message"]
        assert "補給演練目標：每小時 40-60g 碳水" in user_message
        assert "碳水 45g" in user_message

    def test_fallback_plan_contains_fuel_target(
        self, empty_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        """AI 不可用時，fallback 計劃仍要有確定性的補給演練目標。"""
        from datetime import timedelta

        race = date.today() + timedelta(weeks=6)  # 5-8 週 → 60-75g/hr
        with patch("scripts.training_advisor._get_race_date", return_value=race):
            result = _build_fallback_plan(empty_context, sample_profile)

        assert "補給演練目標" in result
        assert "60-75g" in result
        assert "/fuel" in result

    def test_coach_fallback_plan_contains_fuel_target(
        self, empty_context: WeeklyContext, sample_profile: AthleteProfile
    ) -> None:
        coach = GarminCoachContext(
            plan_name="Garmin Coach 馬拉松",
            current_phase="BUILD",
            current_phase_cn="建量期",
            weeks_total=16,
            race_date="2026-10-25",
            tasks=(),
        )
        result = _build_fallback_plan(empty_context, sample_profile, coach=coach)
        assert "補給演練目標" in result

    def test_dry_run_output_contains_fuel_target_with_fake_fuel_data(
        self, fueled_context: WeeklyContext
    ) -> None:
        """驗收情境：training_advisor --dry-run 輸出含補給演練目標
        （餵假的近期 Fuel 數據；AI 不可用 → fallback 路徑，全 mock 在 API 邊界）。"""
        with (
            patch.dict("os.environ", {
                "NOTION_API_KEY": "test-key",
                "HEALTH_DB_ID": "db-health",
                "ACTIVITY_DB_ID": "db-activity",
                "GARMIN_COACH_PLAN_ID": "",
                "ANTHROPIC_API_KEY": "",
            }),
            patch("scripts.training_advisor.get_notion_client", return_value=MagicMock()),
            patch("scripts.training_advisor.fetch_health_history", return_value=[]),
            patch("scripts.training_advisor.fetch_activity_history", return_value=[]),
            patch("scripts.training_advisor.build_weekly_context", return_value=fueled_context),
            patch("scripts.training_advisor._generate_weekly_plan", return_value=None),
            patch("scripts.telegram_bot.safe_send_alert") as mock_alert,
        ):
            result = run_weekly_advisor(dry_run=True)

        assert result is not None
        assert "補給演練目標" in result
        assert "/fuel" in result
        mock_alert.assert_called_once()  # AI 不可用要告警，但不打真 Telegram


# ── TestRunWeeklyAdvisor ──────────────────────────────────────────────────────


class TestRunWeeklyAdvisor:
    def test_returns_none_when_notion_api_key_missing(self) -> None:
        with patch.dict("os.environ", {"NOTION_API_KEY": "", "HEALTH_DB_ID": "db-health"}):
            result = run_weekly_advisor(dry_run=True)
        assert result is None

    def test_returns_none_when_health_db_id_missing(self) -> None:
        with patch.dict("os.environ", {
            "NOTION_API_KEY": "test-key",
            "HEALTH_DB_ID": "",
        }):
            result = run_weekly_advisor(dry_run=True)
        assert result is None

    def test_dry_run_returns_plan_text(self) -> None:
        mock_context = WeeklyContext(
            days=7,
            health_records=(),
            activity_records=(),
            total_distance_km=28.0,
            total_training_load=200.0,
            run_count=3,
            strength_count=2,
            avg_sleep_score=78.0,
            avg_hrv=52.0,
            avg_resting_hr=58.0,
            chronic_load=190.0,
        )

        with (
            patch.dict("os.environ", {
                "NOTION_API_KEY": "test-key",
                "HEALTH_DB_ID": "db-health",
                "ACTIVITY_DB_ID": "",
                "ANTHROPIC_API_KEY": "",
                "GARMIN_COACH_PLAN_ID": "",  # 避免真實 Garmin 呼叫（見 test-hygiene 事故記錄）
            }),
            patch("scripts.training_advisor.get_notion_client", return_value=MagicMock()),
            patch("scripts.training_advisor.fetch_health_history", return_value=[]),
            patch("scripts.training_advisor.fetch_activity_history", return_value=[]),
            patch("scripts.training_advisor.build_weekly_context", return_value=mock_context),

            patch("scripts.training_advisor._generate_weekly_plan", return_value=None),
        ):
            result = run_weekly_advisor(dry_run=True)

        assert result is not None
        # ANTHROPIC_API_KEY 未設定 → 應使用 fallback 計劃
        assert "Fallback" in result or "fallback" in result.lower() or "API" in result

    def test_skips_activity_fetch_when_db_not_set(self) -> None:
        mock_context = WeeklyContext(
            days=7,
            health_records=(),
            activity_records=(),
            total_distance_km=0.0,
            total_training_load=0.0,
            run_count=0,
            strength_count=0,
            avg_sleep_score=None,
            avg_hrv=None,
            avg_resting_hr=None,
            chronic_load=None,
        )

        with (
            patch.dict("os.environ", {
                "NOTION_API_KEY": "test-key",
                "HEALTH_DB_ID": "db-health",
                "ACTIVITY_DB_ID": "",   # 未設定
                "ANTHROPIC_API_KEY": "",
                "GARMIN_COACH_PLAN_ID": "",  # 避免真實 Garmin 呼叫（見 test-hygiene 事故記錄）
            }),
            patch("scripts.training_advisor.get_notion_client", return_value=MagicMock()),
            patch("scripts.training_advisor.fetch_health_history", return_value=[]),
            patch("scripts.training_advisor.fetch_activity_history") as mock_fetch_act,
            patch("scripts.training_advisor.build_weekly_context", return_value=mock_context),
            patch("scripts.training_advisor._generate_weekly_plan", return_value=None),
        ):
            run_weekly_advisor(dry_run=True)

        # ACTIVITY_DB_ID 未設定時，不應呼叫 fetch_activity_history
        mock_fetch_act.assert_not_called()

    def test_uses_claude_plan_when_api_available(self) -> None:
        """當 Claude API 可用時，應使用 AI 計劃而非 fallback。"""
        mock_context = WeeklyContext(
            days=7,
            health_records=(),
            activity_records=(),
            total_distance_km=30.0,
            total_training_load=210.0,
            run_count=3,
            strength_count=2,
            avg_sleep_score=79.0,
            avg_hrv=51.0,
            avg_resting_hr=57.0,
            chronic_load=200.0,
        )
        ai_plan = "🏃 **下週訓練計劃** — AI 生成版本"

        with (
            patch.dict("os.environ", {
                "NOTION_API_KEY": "test-key",
                "HEALTH_DB_ID": "db-health",
                "ACTIVITY_DB_ID": "db-activity",
                "ANTHROPIC_API_KEY": "sk-test",
                "GARMIN_COACH_PLAN_ID": "",  # 避免真實 Garmin 呼叫（見 test-hygiene 事故記錄）
            }),
            patch("scripts.training_advisor.get_notion_client", return_value=MagicMock()),
            patch("scripts.training_advisor.fetch_health_history", return_value=[]),
            patch("scripts.training_advisor.fetch_activity_history", return_value=[]),
            patch("scripts.training_advisor.build_weekly_context", return_value=mock_context),

            patch("scripts.training_advisor._generate_weekly_plan", return_value=ai_plan),
        ):
            result = run_weekly_advisor(dry_run=True)

        assert result == ai_plan

    def test_passes_rolling_7day_resting_hr_to_generate_weekly_plan(self) -> None:
        """T13：health_records 的近 7 天安靜心率均值要傳給 _generate_weekly_plan。"""
        mock_context = WeeklyContext(
            days=7,
            health_records=(),
            activity_records=(),
            total_distance_km=0.0,
            total_training_load=0.0,
            run_count=0,
            strength_count=0,
            avg_sleep_score=None,
            avg_hrv=None,
            avg_resting_hr=None,
            chronic_load=None,
        )
        health_records = [
            HealthData(resting_heart_rate=70),
            HealthData(resting_heart_rate=68),
        ]

        with (
            patch.dict("os.environ", {
                "NOTION_API_KEY": "test-key",
                "HEALTH_DB_ID": "db-health",
                "ACTIVITY_DB_ID": "",
                "ANTHROPIC_API_KEY": "",
                "GARMIN_COACH_PLAN_ID": "",  # 避免真實 Garmin 呼叫（見 test-hygiene 事故記錄）
            }),
            patch("scripts.training_advisor.get_notion_client", return_value=MagicMock()),
            patch("scripts.training_advisor.fetch_health_history", return_value=health_records),
            patch("scripts.training_advisor.fetch_activity_history", return_value=[]),
            patch("scripts.training_advisor.build_weekly_context", return_value=mock_context),
            patch(
                "scripts.training_advisor._generate_weekly_plan", return_value="計劃"
            ) as mock_gen,
        ):
            run_weekly_advisor(dry_run=True)

        assert mock_gen.call_args.kwargs["resting_hr"] == 69.0
