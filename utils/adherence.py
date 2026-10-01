"""utils/adherence.py — 依 logs/advice_log.jsonl + Activity DB 計算建議遵循率（T15）。

系統目前無法回答「AI 建議有沒有被遵循？」。評審裁定：用數據計算，不用按鈕
——Garmin 本來就記錄實際訓練，零使用者摩擦。本模組是純程式計算（不經 AI）：

1. ``daily_adjust.py`` 每天寫一行結構化建議判定到 ``logs/advice_log.jsonl``
   （``verdict``：rest/reduce/maintain，見 ``scripts/daily_adjust._verdict_to_category``）。
2. 本模組讀回這些紀錄，比對當天 Activity DB 實際訓練強度，判斷「有沒有照建議做」。

**關鍵限制（務必遵守）**：Activity DB 由 ``health_tracker_web.py`` 手動觸發
寫入，目前無排程，任何一天完全沒有 Activity 列都必須視為「無法判定」
（排除分母），不可當作遵循或違反——沒有列可能代表『真的休息』，也可能代表
『追蹤器那天沒跑，資料還沒進來』，兩者無法區分，寧可少算也不要誤判。
遵循率因此只在 health_tracker_web 有跑的日子有意義，輸出必須註明樣本數。

**每日實際強度分級（啟發式，粗糙但方向正確）**：優先用 training_load 加總，
其次用當日最高 RPE，最後用總時長，分成 rest / reduce / maintain 三級
（閾值見下方常數，可依實際跑者調整）。
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from models import ActivityData

logger = logging.getLogger(__name__)

_ADVICE_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "advice_log.jsonl"

# 每日實際強度分級閾值（啟發式）。training_load 是 Garmin 的 EPOC 訓練負荷，
# 沒有官方「rest/reduce/maintain」分界，這裡取跑者實務上的粗略經驗值。
_REST_TRAINING_LOAD_MAX = 30.0
_REDUCE_TRAINING_LOAD_MAX = 90.0
_REST_RPE_MAX = 3
_REDUCE_RPE_MAX = 6
_REST_DURATION_MAX_MIN = 20.0
_REDUCE_DURATION_MAX_MIN = 45.0

_KNOWN_VERDICTS = ("rest", "reduce", "maintain")


# ── 資料結構 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdviceLogEntry:
    """單日的建議判定紀錄（``read_advice_log`` 的解析結果）。

    Attributes:
        date: 日期（ISO 字串，YYYY-MM-DD）。
        verdict: 建議類別，``"rest"`` / ``"reduce"`` / ``"maintain"`` 之一。
        key_metric: 觸發依據（供人工稽核，非計算必要）。
        message_hash: 當日建議訊息的雜湊（供未來去重/比對用）。
    """

    date: str
    verdict: str
    key_metric: Optional[str] = None
    message_hash: Optional[str] = None


@dataclass(frozen=True)
class AdherenceResult:
    """一週遵循率計算結果。

    Attributes:
        judged_days: 可判定的天數（有建議紀錄 **且** 當天有 Activity 列）。
        adherent_days: 上述天數中，判定為「有遵循」的天數。
        excluded_no_activity: 有建議紀錄但當天無 Activity 列、被排除分母的天數。
        rest_judged / rest_adherent: 「休息建議」的可判定天數／遵循天數。
        reduce_judged / reduce_adherent: 「降載建議」的可判定天數／遵循天數。
        maintain_judged / maintain_adherent: 「維持建議」的可判定天數／遵循天數。
    """

    judged_days: int
    adherent_days: int
    excluded_no_activity: int
    rest_judged: int
    rest_adherent: int
    reduce_judged: int
    reduce_adherent: int
    maintain_judged: int
    maintain_adherent: int


# ── 讀取 advice_log ──────────────────────────────────────────────────────────


def read_advice_log(
    week_start: date,
    week_end: date,
    log_path: Optional[Path] = None,
) -> List[AdviceLogEntry]:
    """讀取 ``logs/advice_log.jsonl`` 中落在 ``[week_start, week_end]``（含首尾）的紀錄。

    同一天若有多行（例如 daily_adjust 當天重跑過），只取最後一行視為當天最終建議。
    檔案不存在、單行 JSON 解析失敗、或 ``verdict`` 不是已知類別的行一律跳過
    並記警告，不拋出例外——這是事後統計腳本，資料不完美是常態。

    Args:
        week_start: 週起始日（含）。
        week_end: 週結束日（含）。
        log_path: 覆寫 log 檔路徑（供測試注入），預設為
            ``logs/advice_log.jsonl``。

    Returns:
        依日期排序（舊到新）的 ``AdviceLogEntry`` 清單；查無資料時回傳空清單。
    """
    path = log_path or _ADVICE_LOG_PATH
    if not path.exists():
        logger.info("advice_log 不存在（%s），遵循率無資料", path)
        return []

    by_date: Dict[str, AdviceLogEntry] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("advice_log 第 %d 行解析失敗，跳過", line_no)
                continue

            entry_date = payload.get("date")
            if not entry_date:
                continue
            try:
                parsed_date = date.fromisoformat(entry_date)
            except ValueError:
                logger.warning("advice_log 第 %d 行日期格式錯誤，跳過", line_no)
                continue
            if not (week_start <= parsed_date <= week_end):
                continue

            verdict = payload.get("verdict")
            if verdict not in _KNOWN_VERDICTS:
                continue

            by_date[entry_date] = AdviceLogEntry(
                date=entry_date,
                verdict=verdict,
                key_metric=payload.get("key_metric"),
                message_hash=payload.get("message_hash"),
            )

    return sorted(by_date.values(), key=lambda e: e.date)


# ── 當日實際強度分級 ─────────────────────────────────────────────────────────


def classify_day_effort(activities: List[ActivityData]) -> str:
    """把單日 Activity 紀錄分成 rest / reduce / maintain 三級。

    優先序：training_load 加總 → 當日最高 RPE → 總時長。任一層有資料就用該層
    判定，不往下退（避免不同量尺互相稀釋）。

    呼叫端必須保證 ``activities`` 非空——空清單代表「當天無 Activity 列」，
    這是「無法判定」，應在呼叫前就被排除分母（見 ``compute_adherence``），
    不該傳進來當「rest」判定。

    Args:
        activities: 當天的 ActivityData 清單（非空）。

    Returns:
        ``"rest"`` / ``"reduce"`` / ``"maintain"``。
    """
    loads = [a.training_load for a in activities if a.training_load is not None]
    if loads:
        total_load = sum(loads)
        if total_load <= _REST_TRAINING_LOAD_MAX:
            return "rest"
        if total_load <= _REDUCE_TRAINING_LOAD_MAX:
            return "reduce"
        return "maintain"

    rpes = [a.rpe for a in activities if a.rpe is not None]
    if rpes:
        peak_rpe = max(rpes)
        if peak_rpe <= _REST_RPE_MAX:
            return "rest"
        if peak_rpe <= _REDUCE_RPE_MAX:
            return "reduce"
        return "maintain"

    durations = [a.duration_min for a in activities if a.duration_min is not None]
    total_duration = sum(durations) if durations else 0.0
    if total_duration <= _REST_DURATION_MAX_MIN:
        return "rest"
    if total_duration <= _REDUCE_DURATION_MAX_MIN:
        return "reduce"
    return "maintain"


def _is_adherent(verdict: str, actual: str) -> bool:
    """判斷建議 verdict 與當日實際分級是否算「遵循」。

    - ``rest``：上限型建議，必須真的休息（``actual == "rest"``）才算遵循。
    - ``reduce``：實際 <= reduce 都算遵循（休息比降載更保守，不算違反）。
    - ``maintain``：下限型建議，至少要有 reduce 等級的參與才算遵循
      （``actual == "rest"`` 代表當天雖有 Activity 列，但強度趨近於零，
      視為「沒照建議維持」）。

    Args:
        verdict: 建議類別。
        actual: 當日實際分級（見 ``classify_day_effort``）。

    Returns:
        是否算遵循；``verdict`` 不是已知類別時保守回傳 False。
    """
    if verdict == "rest":
        return actual == "rest"
    if verdict == "reduce":
        return actual in ("rest", "reduce")
    if verdict == "maintain":
        return actual in ("reduce", "maintain")
    return False


# ── 週遵循率計算 ─────────────────────────────────────────────────────────────


def compute_adherence(
    advice_entries: List[AdviceLogEntry],
    activities_by_date: Dict[str, List[ActivityData]],
) -> AdherenceResult:
    """依建議紀錄 + 當日實際 Activity 計算一週遵循率。

    Args:
        advice_entries: 本週的建議紀錄（見 ``read_advice_log``）。
        activities_by_date: 日期字串（YYYY-MM-DD）→ 當天 Activity 清單。
            當天沒有對應 key 或清單為空 → 該天排除分母（見模組 docstring）。

    Returns:
        AdherenceResult。
    """
    judged = 0
    adherent = 0
    excluded = 0
    rest_judged = rest_adherent = 0
    reduce_judged = reduce_adherent = 0
    maintain_judged = maintain_adherent = 0

    for entry in advice_entries:
        activities = activities_by_date.get(entry.date) or []
        if not activities:
            excluded += 1
            continue

        actual = classify_day_effort(activities)
        is_adherent_today = _is_adherent(entry.verdict, actual)

        judged += 1
        if is_adherent_today:
            adherent += 1

        if entry.verdict == "rest":
            rest_judged += 1
            rest_adherent += int(is_adherent_today)
        elif entry.verdict == "reduce":
            reduce_judged += 1
            reduce_adherent += int(is_adherent_today)
        elif entry.verdict == "maintain":
            maintain_judged += 1
            maintain_adherent += int(is_adherent_today)

    return AdherenceResult(
        judged_days=judged,
        adherent_days=adherent,
        excluded_no_activity=excluded,
        rest_judged=rest_judged,
        rest_adherent=rest_adherent,
        reduce_judged=reduce_judged,
        reduce_adherent=reduce_adherent,
        maintain_judged=maintain_judged,
        maintain_adherent=maintain_adherent,
    )


def format_adherence_line(result: AdherenceResult) -> Optional[str]:
    """把 ``AdherenceResult`` 格式化為一行中性、非責備語氣的 Telegram 文字。

    Args:
        result: ``compute_adherence`` 的計算結果。

    Returns:
        一行文字；``judged_days == 0``（本週完全無法判定）時回傳 None，
        呼叫端應跳過此行，而非印出誤導性的 ``0/0``。
    """
    if result.judged_days == 0:
        return None

    detail_bits = []
    if result.rest_judged:
        detail_bits.append(f"休息建議 {result.rest_adherent}/{result.rest_judged}")
    if result.reduce_judged:
        detail_bits.append(f"降載 {result.reduce_adherent}/{result.reduce_judged}")
    if result.maintain_judged:
        detail_bits.append(f"維持 {result.maintain_adherent}/{result.maintain_judged}")

    line = f"📊 本週遵循 {result.adherent_days}/{result.judged_days}"
    if detail_bits:
        line += "（" + "，".join(detail_bits) + "）"
    if result.excluded_no_activity:
        line += f"｜{result.excluded_no_activity} 天無 Activity 資料排除"
    line += "（啟發式估算，非精確值，僅供參考，非考核）"
    return line
