"""utils/fueling.py — 長跑補給與腸胃訓練協議的純函式工具（T11）。

這是可配置的補給演練規則範例，不代表任何人的病史或個人處方。
公開版保留確定性計算與格式化；實際使用前需由合適的專業人員檢視：

- ``carb_target_g_per_hr``：依距賽週數回傳本週補給演練目標（g/hr 區間）
- ``fuel_target_line``：長跑日早晨訊息用的一行補給目標（daily_adjust）
- ``format_fuel_history``：最近 /fuel 回報的 prompt 區塊（training_advisor）

全部是純函式（除了讀 ``RACE_DATE`` 環境變數），不打任何外部 API。

使用方式::

    from utils.fueling import carb_target_g_per_hr, fuel_target_line

    lo, hi = carb_target_g_per_hr(weeks_left=10)   # (40, 60)
    line = fuel_target_line()                       # 長跑日訊息附加行
"""

import os
import re
from datetime import date
from typing import List, Optional, Tuple

from models import ActivityData

# 範例賽事攝取區間（g 碳水 / 小時），不是個人建議。
RACE_CARBS_MIN_G_PER_HR = 60
RACE_CARBS_MAX_G_PER_HR = 90

# 「長跑」定義：時間 ≥90 分鐘，或（無時間估計時）距離 ≥14km
# 此為範例閾值，部署前需依個人情境調整。
LONG_RUN_MIN_DURATION_MIN = 90.0
LONG_RUN_MIN_DISTANCE_KM = 14.0

_DEFAULT_RACE_DATE = date(2030, 1, 1)  # Synthetic example; configure RACE_DATE privately.


def get_race_date() -> date:
    """讀取目標賽事日期（``RACE_DATE``，格式非法或未設定時用預設值）。

    Returns:
        賽事日期（公開版使用合成示範日期）。
    """
    raw = os.getenv("RACE_DATE", _DEFAULT_RACE_DATE.isoformat())
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return _DEFAULT_RACE_DATE


def weeks_to_race(today: Optional[date] = None) -> int:
    """計算距賽事的完整週數（與 training_advisor 同一算法，最小 0）。

    Args:
        today: 基準日（預設今天）。

    Returns:
        距賽週數（``(race - today).days // 7``，不為負）。
    """
    today = today or date.today()
    return max(0, (get_race_date() - today).days // 7)


def carb_target_g_per_hr(weeks_left: int) -> Tuple[int, int]:
    """依距賽週數回傳本週長跑補給演練目標（每小時碳水 g 區間）。

    漸進協議（從 30–40g/hr 練到賽事目標 60–90g/hr）：

    - 距賽 ≥13 週：30–40 g/hr（腸胃訓練起步）
    - 距賽 9–12 週：40–60 g/hr
    - 距賽 5–8 週：60–75 g/hr（進入賽事下限強度）
    - 距賽 ≤4 週：60–90 g/hr（完整賽事演練，含賽日計劃）

    Args:
        weeks_left: 距賽週數。

    Returns:
        ``(下限, 上限)`` g/hr 元組。
    """
    if weeks_left >= 13:
        return (30, 40)
    if weeks_left >= 9:
        return (40, 60)
    if weeks_left >= 5:
        return (60, 75)
    return (RACE_CARBS_MIN_G_PER_HR, RACE_CARBS_MAX_G_PER_HR)


def is_long_run(
    duration_min: Optional[float],
    distance_km: Optional[float],
) -> bool:
    """判斷一筆訓練是否算「長跑」（補給演練適用對象）。

    時間 ≥90 分鐘即算；沒有時間估計時退而以距離 ≥14km 判斷。
    兩者皆缺（None）時回傳 False — Activity/Coach 資料可能過期或缺欄，
    「不確定」時寧可不觸發補給提醒，也不要每天都喊狼來了。

    Args:
        duration_min: 訓練時間（分鐘），可為 None。
        distance_km: 訓練距離（km），可為 None。

    Returns:
        是否為長跑。
    """
    if duration_min is not None and duration_min >= LONG_RUN_MIN_DURATION_MIN:
        return True
    if distance_km is not None and distance_km >= LONG_RUN_MIN_DISTANCE_KM:
        return True
    return False


# 從自由格式排程描述文字擷取距離/時間的寬鬆 regex（T17：coach_week.json 手動
# 抄錄的 target 欄位沒有結構化距離/時間欄位，只能從文字猜）。
_DISTANCE_KM_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*km", re.IGNORECASE)
_DURATION_MIN_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:min|分鐘|分)", re.IGNORECASE)


def parse_workout_text(text: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """從自由格式的排程描述文字擷取距離（km）與時間（分鐘）估計值。

    供 coach_week.json（T12 手動課表備援，見 ``scripts.training_advisor.
    _load_manual_coach_week``）這類沒有結構化 ``estimated_distance_m`` /
    ``estimated_duration_s`` 欄位的排程來源使用，讓 ``is_long_run`` 能沿用
    同一套判斷邏輯（T17：明日長跑前瞻）。只做簡單 regex 擷取，抓不到就是
    None——不強行解析每一種可能格式（過度工程），寧可漏判也不要誤判。

    Args:
        text: 排程描述文字（例如 ``"18km @ 7:00/km"``、``"90min easy"``），
            可為 None。

    Returns:
        ``(duration_min, distance_km)``；任一項擷取不到時為 None。
    """
    if not text:
        return (None, None)

    duration_min: Optional[float] = None
    distance_km: Optional[float] = None

    m = _DURATION_MIN_PATTERN.search(text)
    if m:
        duration_min = float(m.group(1))

    m = _DISTANCE_KM_PATTERN.search(text)
    if m:
        distance_km = float(m.group(1))

    return (duration_min, distance_km)


def fuel_target_line(today: Optional[date] = None) -> str:
    """組出長跑日早晨訊息用的一行補給演練目標（含量化數字）。

    Args:
        today: 基準日（預設今天）。

    Returns:
        一行文字，例如
        ``⛽ 今日長跑補給演練（距賽 15 週）：每小時 30-40g 碳水 + 電解質水，
        跑後回報 /fuel <碳水g> <GI 1-5> [內容]``。
    """
    weeks_left = weeks_to_race(today)
    lo, hi = carb_target_g_per_hr(weeks_left)
    return (
        f"⛽ 今日長跑補給演練（距賽 {weeks_left} 週）："
        f"每小時 {lo}-{hi}g 碳水 + 電解質水（腸胃訓練，賽事目標 "
        f"{RACE_CARBS_MIN_G_PER_HR}-{RACE_CARBS_MAX_G_PER_HR}g/hr）。"
        f"跑後回報 /fuel <碳水g> <GI 1-5> [內容]"
    )


def format_fuel_history(
    activity_records: List[ActivityData],
    limit: int = 5,
) -> str:
    """把最近的 /fuel 回報格式化為 AI prompt 用的文字區塊。

    只取含有 ``fuel_carbs_g`` 或 ``gi_score`` 的紀錄（最多 ``limit`` 筆，
    輸入清單假定已依日期新到舊排序）。無資料時明說「無回報」而非留白 —
    Activity DB 可能過期或跑者尚未用過 /fuel，這是常態不是例外。

    Args:
        activity_records: 活動紀錄清單（依日期新到舊）。
        limit: 最多列出幾筆。

    Returns:
        多行文字區塊，第一行固定為「最近長跑補給回報（/fuel）：」。
    """
    fueled = [
        r
        for r in activity_records
        if r.fuel_carbs_g is not None or r.gi_score is not None
    ]

    if not fueled:
        return "最近長跑補給回報（/fuel）：無（尚未回報過賽中補給）"

    lines = ["最近長跑補給回報（/fuel）："]
    for r in fueled[:limit]:
        day = (r.start_time or "?")[:10]
        parts = []
        if r.distance_km:
            parts.append(f"{r.distance_km}km")
        if r.duration_min:
            parts.append(f"{r.duration_min:.0f}min")
        if r.fuel_carbs_g is not None:
            carbs = f"碳水 {r.fuel_carbs_g:.0f}g"
            if r.duration_min:
                rate = r.fuel_carbs_g / (r.duration_min / 60.0)
                carbs += f"（約 {rate:.0f}g/hr）"
            parts.append(carbs)
        if r.gi_score is not None:
            marker = "⚠️" if r.gi_score >= 3 else ""
            parts.append(f"GI {r.gi_score}{marker}")
        if r.fuel_plan:
            parts.append(f"內容：{r.fuel_plan}")
        lines.append(f"- {day}: {' / '.join(parts)}")

    return "\n".join(lines)
