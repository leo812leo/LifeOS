"""utils/sleep.py — 睡眠一級管理：睡眠債計算與就寢建議（T18）。

通用睡眠追蹤工具，不包含私人訪談或實際健康紀錄。
就寢提醒與隔日強度調整需要使用者自行確認設定。

本模組提供：

- ``sleep_debt_7d``：近 7 天累積睡眠債（純函式，供規則引擎與週回顧共用）
- ``build_sleep_section``：組出就寢建議區段的字串（供 T17 日報③區段使用；
  介面刻意設計為「回傳字串的純函式」，T17 尚未完成前可獨立測試/使用）

輸入來源全部是 Garmin 被動同步的 Health DB 資料（``sleep_hours`` /
``sleep_score``），不新增任何需要使用者手動輸入的欄位。

語氣定調：中性教練，不責備——「睡眠優先」而非「你又熬夜」；即使沒有觸發
降級門檻，就寢建議一樣顯示（管理是常態，不是懲罰）。

使用方式::

    from utils.sleep import sleep_debt_7d, build_sleep_section

    debt = sleep_debt_7d(health_records)             # 近 7 天累積睡眠債（小時）
    section = build_sleep_section(health_records)     # 就寢建議區段文字
"""

import os
from datetime import date, datetime, time as dt_time, timedelta
from typing import List, Optional

from models import HealthData, RuleConfig

# 每日睡眠目標（小時）。使用者每晚應睡滿的時數，睡眠債＝目標與實際的落差。
_DEFAULT_SLEEP_TARGET_H = 7.0

# 起床需求（"HH:MM"）。以下為示範預設值，使用者須確認實際作息。
_DEFAULT_WEEKDAY_WAKE = "06:30"
_DEFAULT_WEEKEND_RUN_START = "06:00"

# 訓練結束到就寢的消化緩衝（小時）——晚餐後/賽前補給後不宜緊接著躺平。
_TRAINING_DIGESTION_BUFFER_H = 2.0

# 睡眠債計算的預設回溯天數。
_DEFAULT_WINDOW = 7

# 用來做時間加減的任意錨定日期（只取 .time()，日期本身無意義）。
_ANCHOR_DATE = date(2000, 1, 1)


# ── 環境變數讀取 ─────────────────────────────────────────────────────────────


def get_sleep_target_h() -> float:
    """讀取每日睡眠目標時數（``SLEEP_TARGET_H``）。

    Returns:
        睡眠目標小時數；環境變數未設定或無法解析為浮點數時回傳預設值 7.0。
    """
    raw = os.getenv("SLEEP_TARGET_H", "")
    if not raw:
        return _DEFAULT_SLEEP_TARGET_H
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_SLEEP_TARGET_H


def _parse_hhmm(raw: str, fallback: str) -> dt_time:
    """把 ``"HH:MM"`` 字串解析為 ``time``，解析失敗時退回 ``fallback``。"""
    try:
        hour_str, minute_str = raw.split(":")
        return dt_time(int(hour_str), int(minute_str))
    except (ValueError, AttributeError):
        hour_str, minute_str = fallback.split(":")
        return dt_time(int(hour_str), int(minute_str))


def get_weekday_wake() -> dt_time:
    """讀取平日起床時間（``WEEKDAY_WAKE``，格式 ``"HH:MM"``）。

    Returns:
        平日起床時間；未設定或格式錯誤時回傳預設值 06:30。
    """
    raw = os.getenv("WEEKDAY_WAKE", "")
    return _parse_hhmm(raw, _DEFAULT_WEEKDAY_WAKE)


def get_weekend_run_start() -> dt_time:
    """讀取週末長跑出門時間（``WEEKEND_RUN_START``，格式 ``"HH:MM"``）。

    Returns:
        週末出門時間；未設定或格式錯誤時回傳預設值 06:00。
    """
    raw = os.getenv("WEEKEND_RUN_START", "")
    return _parse_hhmm(raw, _DEFAULT_WEEKEND_RUN_START)


# ── 時間運算 ─────────────────────────────────────────────────────────────────


def _shift_time(t: dt_time, delta_hours: float) -> dt_time:
    """把 ``time`` 加減指定小時數，跨午夜正確捲動（純函式）。

    Args:
        t: 基準時間。
        delta_hours: 加減的小時數（可為負數、可為小數）。

    Returns:
        位移後的 ``time``（只取時分，捲動跨日不影響結果）。
    """
    base = datetime.combine(_ANCHOR_DATE, t)
    shifted = base + timedelta(hours=delta_hours)
    return shifted.time()


def _fmt_time(t: dt_time) -> str:
    """把 ``time`` 格式化為 ``"HH:MM"``。"""
    return t.strftime("%H:%M")


# ── 睡眠債計算 ───────────────────────────────────────────────────────────────


def sleep_debt_7d(
    health_records: List[HealthData],
    window: int = _DEFAULT_WINDOW,
    target_h: Optional[float] = None,
) -> Optional[float]:
    """計算近 N 天（預設 7）累積睡眠債（小時）。

    ``health_debt = Σ max(0, target_h - actual_i)``，i 為近 ``window`` 天中
    「有睡眠資料」的天數。

    **缺資料天數的處理（啟發式，刻意設計）**：``health_records`` 假設已依
    ``notion_reader.fetch_health_history`` 的慣例由新到舊排序，直接取前
    ``window`` 筆視為「近 N 天」。若某天 ``sleep_hours`` 為 ``None``（手錶
    未同步/漏戴），該天**不計入債務**（貢獻 0，而非視為當天完全沒睡的
    最大債務）——沒有資料不代表沒睡，寧可低估債務也不要因為追蹤器缺漏
    而誤判使用者狀態。

    Args:
        health_records: 健康紀錄清單（新到舊排序），通常來自
            ``scripts.notion_reader.fetch_health_history``。
        window: 回溯天數（預設 7）。
        target_h: 覆寫睡眠目標時數（供測試/呼叫端注入），預設 None 時讀取
            ``get_sleep_target_h()``。

    Returns:
        累積睡眠債（小時，四捨五入至小數 1 位）；窗口內完全沒有任何一天
        有睡眠資料時回傳 ``None``（資料不足，無法判斷，而非債務為 0）。
    """
    target = target_h if target_h is not None else get_sleep_target_h()
    recent = health_records[:window]
    valid_hours = [h.sleep_hours for h in recent if h.sleep_hours is not None]
    if not valid_hours:
        return None
    debt = sum(max(0.0, target - h) for h in valid_hours)
    return round(debt, 1)


def _is_sleep_priority(
    last_night_h: Optional[float],
    debt: Optional[float],
    config: RuleConfig,
) -> bool:
    """判斷是否觸發「睡眠優先」提示（與 ``rule_engine`` Rule 3 同一條件）。

    Args:
        last_night_h: 昨晚睡眠時數（可為 None）。
        debt: 近 7 天累積睡眠債（可為 None）。
        config: 規則閾值設定（單一來源，見 ``models.RuleConfig``）。

    Returns:
        7 日睡眠債 > ``sleep_debt_downgrade_hours`` 或昨晚 <
        ``sleep_acute_downgrade_hours`` 時回傳 True。
    """
    if debt is not None and debt > config.sleep_debt_downgrade_hours:
        return True
    if last_night_h is not None and last_night_h < config.sleep_acute_downgrade_hours:
        return True
    return False


# ── 就寢建議區段 ─────────────────────────────────────────────────────────────


def build_sleep_section(
    health_records: List[HealthData],
    today: Optional[date] = None,
    config: Optional[RuleConfig] = None,
) -> str:
    """組出就寢建議區段文字（供 T17 日報③區段使用）。

    內容恆包含（管理是常態，不是懲罰——即使未觸發睡眠優先，就寢目標一樣
    顯示）：

    - 觸發「睡眠優先」時最前面加一行提示（本週睡眠債 X.Xh）
    - 昨晚睡眠時數 / 分數（``health_records[0]``，缺資料時明說「無資料」）
    - 7 日睡眠債（有資料才顯示）
    - 今晚就寢目標＝明日起床需求 − 睡眠目標時數
    - 週間（今天為週一至週五）加一行訓練結束時間建議
      （＝就寢目標 − 2h 消化緩衝）

    Args:
        health_records: ``fetch_health_history`` 回傳的健康紀錄（新到舊
            排序，index 0 為「昨晚」）。
        today: 基準日（預設今天），決定明日是否為週末（起床需求）與今天
            是否為平日晚練日。
        config: 規則閾值設定（睡眠優先觸發門檻的單一來源），預設
            ``RuleConfig()``。

    Returns:
        多行文字（``\\n`` 分隔）。
    """
    today = today or date.today()
    config = config or RuleConfig()
    tomorrow = today + timedelta(days=1)

    last_night = health_records[0] if health_records else None
    last_sleep_h = last_night.sleep_hours if last_night else None
    last_sleep_score = last_night.sleep_score if last_night else None

    debt = sleep_debt_7d(health_records)

    is_weekend_tomorrow = tomorrow.weekday() >= 5  # 5=Sat, 6=Sun
    wake_time = get_weekend_run_start() if is_weekend_tomorrow else get_weekday_wake()
    target_h = get_sleep_target_h()
    bedtime = _shift_time(wake_time, -target_h)

    lines: List[str] = []

    if _is_sleep_priority(last_sleep_h, debt, config):
        debt_str = f"{debt:.1f}h" if debt is not None else "?"
        lines.append(f"😴 睡眠優先：本週睡眠債 {debt_str}")

    if last_sleep_h is not None:
        score_part = f"（分數 {last_sleep_score}）" if last_sleep_score is not None else ""
        lines.append(f"昨晚睡眠：{last_sleep_h}h{score_part}")
    else:
        lines.append("昨晚睡眠：無資料")

    if debt is not None:
        lines.append(f"7 日睡眠債：{debt:.1f}h")

    lines.append(
        f"今晚就寢目標：{_fmt_time(bedtime)}（明早起床需求 {_fmt_time(wake_time)}）"
    )

    if today.weekday() < 5:  # 週一至週五：今晚是晚練日
        cutoff = _shift_time(bedtime, -_TRAINING_DIGESTION_BUFFER_H)
        lines.append(f"今晚訓練請於 {_fmt_time(cutoff)} 前結束以保就寢目標")

    return "\n".join(lines)
