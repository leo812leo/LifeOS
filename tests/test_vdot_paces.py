"""tests/test_vdot_paces.py — utils/vdot_paces 純函式的單元測試（T13）。

驗證重點（見 docs/tasks/T13-rolling-vdot-paces.md 驗收條件）：
- vdot=38 與 vdot=42 的配速表正確、單調（VDOT 越高配速越快，數值越小）。
- Karvonen 心率區間邊界隨 (max_hr, resting_hr) 輸入變動。
- 邊界情況（非正 VDOT、心率儲備非正）明確拋錯，不靜默回傳錯誤數字。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.vdot_paces import (
    DEFAULT_RESTING_HR,
    HeartRateZones,
    PaceTable,
    format_pace,
    karvonen_zones,
    vdot_to_paces,
)


# ── vdot_to_paces ────────────────────────────────────────────────────────────


class TestVdotToPaces:
    def test_returns_pace_table(self) -> None:
        result = vdot_to_paces(38.0)
        assert isinstance(result, PaceTable)

    def test_easy_min_faster_than_easy_max(self) -> None:
        """easy_min 是較快端（數值較小），easy_max 是較慢端（數值較大）。"""
        paces = vdot_to_paces(38.0)
        assert paces.easy_min < paces.easy_max

    def test_pace_ordering_within_single_vdot(self) -> None:
        """同一 VDOT 下，配速由慢到快應為 E > M > T > I > R。"""
        paces = vdot_to_paces(38.0)
        assert paces.easy_max > paces.easy_min > paces.marathon
        assert paces.marathon > paces.threshold
        assert paces.threshold > paces.interval
        assert paces.interval > paces.repetition

    def test_higher_vdot_produces_faster_paces_for_every_zone(self) -> None:
        """VDOT 42 應在每一種訓練強度都比 VDOT 38 快（數值更小）。"""
        p38 = vdot_to_paces(38.0)
        p42 = vdot_to_paces(42.0)
        for field in ("easy_min", "easy_max", "marathon", "threshold", "interval", "repetition"):
            assert getattr(p42, field) < getattr(p38, field), field

    def test_monotonic_across_a_range_of_vdot(self) -> None:
        """配速隨 VDOT 上升單調遞減（越跑越快），涵蓋常見範圍。"""
        vdots = [30, 34, 38, 42, 46, 50, 55, 60]
        marathon_paces = [vdot_to_paces(v).marathon for v in vdots]
        assert marathon_paces == sorted(marathon_paces, reverse=True)

    def test_known_vdot_38_matches_expected_ballpark(self) -> None:
        """VDOT 38（本專案預設值）配速應落在合理區間內（sanity check）。

        數值來自 Daniels & Gilbert 迴歸式反解，非查表照抄；此處只驗證
        「數量級正確」，不要求對到官方查表小數點。
        """
        paces = vdot_to_paces(38.0)
        assert 6.0 < paces.easy_min < 7.5
        assert 6.5 < paces.easy_max < 8.0
        assert 5.0 < paces.marathon < 6.0
        assert 4.5 < paces.interval < 5.5
        assert 4.0 < paces.repetition < 5.0

    def test_non_positive_vdot_raises(self) -> None:
        with pytest.raises(ValueError):
            vdot_to_paces(0)
        with pytest.raises(ValueError):
            vdot_to_paces(-5)


# ── karvonen_zones ───────────────────────────────────────────────────────────


class TestKarvonenZones:
    def test_returns_heart_rate_zones(self) -> None:
        result = karvonen_zones(185, 58)
        assert isinstance(result, HeartRateZones)

    def test_boundaries_strictly_increasing(self) -> None:
        zones = karvonen_zones(185, 58)
        assert zones.z1_max < zones.z2_max < zones.z3_max < zones.z4_max

    def test_default_resting_hr_used_when_omitted(self) -> None:
        assert karvonen_zones(185) == karvonen_zones(185, DEFAULT_RESTING_HR)

    def test_higher_resting_hr_shifts_boundaries_up(self) -> None:
        """安靜心率（RHR）變動時，區間邊界要跟著位移（驗收條件 #2）。"""
        low_rhr = karvonen_zones(185, 55)
        high_rhr = karvonen_zones(185, 70)
        assert high_rhr.z1_max > low_rhr.z1_max
        assert high_rhr.z2_max > low_rhr.z2_max
        assert high_rhr.z3_max > low_rhr.z3_max
        assert high_rhr.z4_max > low_rhr.z4_max

    def test_different_max_hr_shifts_boundaries(self) -> None:
        lower_max = karvonen_zones(175, 58)
        higher_max = karvonen_zones(190, 58)
        assert higher_max.z4_max > lower_max.z4_max

    def test_max_hr_not_greater_than_resting_hr_raises(self) -> None:
        with pytest.raises(ValueError):
            karvonen_zones(55, 58)  # max_hr < resting_hr
        with pytest.raises(ValueError):
            karvonen_zones(58, 58)  # 心率儲備為 0


# ── format_pace ──────────────────────────────────────────────────────────────


class TestFormatPace:
    def test_formats_half_minute(self) -> None:
        assert format_pace(6.5) == "6:30"

    def test_formats_whole_minute(self) -> None:
        assert format_pace(5.0) == "5:00"

    def test_rounds_seconds(self) -> None:
        # 5.999 min → 5 分 59.94 秒 → 四捨五入為 6:00
        assert format_pace(5.999) == "6:00"
