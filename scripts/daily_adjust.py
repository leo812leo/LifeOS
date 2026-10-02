"""scripts/daily_adjust.py — 每日早晨訓練微調建議。

每天早上執行（建議 07:00）：
1. 讀取昨晚睡眠 / HRV / Body Battery / Training Readiness
2. 讀取 Garmin Coach 今天的排程
3. 規則引擎（rule_engine.py 的 8 條確定性規則）先行判定今日強度上限，
   判定結果以結構化區塊注入 AI prompt（T10）
4. AI 判斷：「今天維持原計劃」OR「建議調整為 X，原因 Y」
   — 判定為 REST/EASY 時有程式層防線：AI 仍建議高強度會被覆寫為規則結果
5. 推送至 Telegram

與 training_advisor.py 的差別：
- training_advisor: 每週日，提供整週概覽
- daily_adjust:    每天早上，只看當天 + 昨晚數據，做最後微調

固定四段式日報（T17，單一整合訊息）：
① 今日訓練（AI 結論 + 規則引擎判定；週間「今晚」時態、週末「今早」時態）
② 補給與水分（T11 今日長跑補給 + 明日長跑前瞻，讀不到 coach_week.json 就略過）
③ 睡眠（T18 就寢建議，內容恆附加，管理是常態不是懲罰）
④ 系統（T09 心跳：昨日任務 N/N 成功）
無新訊號日整則壓縮為 T09 的一行心跳格式，不發四段空殼。

使用方式::

    # 推送至 Telegram
    python scripts/daily_adjust.py

    # 乾跑模式
    python scripts/daily_adjust.py --dry-run
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

if TYPE_CHECKING:
    # 只供型別標註用（見 _coach_week_task_is_long_run）；避免在模組載入時就
    # 引入 training_advisor.py 的完整依賴——實際使用處都是函式內 local import。
    from scripts.training_advisor import GarminCoachTask

from config import get_glp1_settings, get_vdot
from models import ActivityData, AthleteProfile, HealthData, RuleConfig, RuleVerdict
from utils.delivery import send_telegram_text
from utils.fueling import (
    carb_target_g_per_hr,
    fuel_target_line,
    is_long_run,
    parse_workout_text,
    weeks_to_race,
)
from utils.logger import get_logger
from utils.run_ledger import append_run, read_recent_runs
from utils.sleep import build_sleep_section, sleep_debt_7d

logger = get_logger(__name__)

# 每日建議記錄檔（供 T15 weekly_review 計算「建議遵循率」用）。
_ADVICE_LOG = Path(__file__).resolve().parent.parent / "logs" / "advice_log.jsonl"

# 規則引擎閾值單一來源（T10）：強度相關門檻一律從 RuleConfig 讀，
# 不在本檔硬編碼，避免與 rule_engine 漂移。
_RULE_CONFIG = RuleConfig()

# Body Battery 低於此值視為 warning 旗標（與規則引擎 Rule 1 同一閾值來源）。
_LOW_BODY_BATTERY_THRESHOLD = _RULE_CONFIG.body_battery_rest

# 規則引擎判定 REST/EASY 時，AI 結論若含這些高強度關鍵字 → 程式層覆寫（T10）。
# 刻意用簡單關鍵字比對（任務要求：不要過度工程）；英文關鍵字以小寫比對。
_HIGH_INTENSITY_KEYWORDS = (
    "間歇",
    "節奏跑",
    "tempo",
    "interval",
    "高強度",
    "衝刺",
    "閾值跑",
    "乳酸閾值",
    "漸速跑",
    "法特雷克",
)

# 允許壓縮成一行心跳的規則等級 — REST/EASY 判定日一律發完整建議（T10）。
_HEARTBEAT_ALLOWED_LEVELS = ("MODERATE", "HIGH")


# ── 主流程 ──────────────────────────────────────────────────────────────


def run_daily_adjust(dry_run: bool = False) -> Optional[str]:
    """執行每日訓練微調建議。

    Returns:
        推送的訊息文字，失敗時回傳 None。
    """
    logger.info("=" * 60)
    logger.info("每日訓練微調 — %s", date.today().isoformat())
    logger.info("=" * 60)

    # ── 1. 讀取 Garmin Coach 今天的排程 ──────────────────────────────────
    today = date.today()
    coach_today = _fetch_today_garmin_coach(today)
    if coach_today is None:
        logger.warning("Garmin Coach 不可用，無今日排程")

    # ── 2. 讀取最新生理數據（昨晚睡眠 + 今早 HRV / Readiness）─────────
    metrics = _fetch_latest_metrics()

    # ── 2b. 讀取最近 3 天 RPE / Niggle 回報（/rpe、/niggle 寫入的訊號）───
    rpe_niggle_records = _fetch_recent_rpe_niggle(days=3)

    # ── 2c. 讀取近 7 天健康紀錄，計算睡眠債（睡眠一級管理，T18）─────────
    health_records = _fetch_recent_health_records()
    debt = sleep_debt_7d(health_records)

    # ── 2d. 規則引擎判定（T10；T18 加入睡眠債）───────────────────────────
    # 確定性規則先行：判定結果以結構化區塊注入 AI prompt；REST/EASY 時
    # 另有程式層防線（見 _enforce_rule_verdict），不只靠 AI 自律。
    verdict = _evaluate_rule_verdict(today, metrics, sleep_debt_7d=debt)
    trigger_reason = ""
    rule_section: Optional[str] = None
    if verdict is not None:
        trigger_reason = _rule_trigger_reason(
            verdict, _metrics_to_health_data(metrics), _RULE_CONFIG, sleep_debt_7d=debt
        )
        rule_section = _format_rule_verdict_section(verdict, trigger_reason)

    # ── 3. AI 判斷 ──────────────────────────────────────────────────────
    message = _generate_daily_adjustment(
        today,
        coach_today,
        metrics,
        rpe_niggle_records,
        verdict=verdict,
        rule_section=rule_section,
    )

    if not message:
        logger.error("無法生成微調建議")
        if not dry_run:
            append_run("daily_adjust.py", ok=False, wrote_notion=False)
        return None

    # ── 3b. 程式層防線（T10）：REST/EASY 判定下 AI 仍建議高強度 → 覆寫 ──
    if verdict is not None:
        message, overridden = _enforce_rule_verdict(message, verdict, trigger_reason)
        if overridden:
            logger.warning(
                "AI 輸出與規則引擎判定衝突，已覆寫為規則結果（%s）",
                verdict.readiness_level,
            )

    # ── 3c. 心跳分流（T09；T10 加規則等級門檻；T11 加長跑補給訊號）──────
    # 「無新訊號」= AI 結論為「維持原計劃」且無任何 warning 旗標
    # （GLP-1 注射日 / 低 Body Battery / Niggle ≥3 / 長跑補給演練日），
    # 且規則引擎判定不是 REST/EASY（降載日必須發完整建議，不可壓成一行）。
    # 此時只發一行心跳，附上昨日各排程是否落地的摘要；否則發完整建議，
    # 並附上規則引擎判定區塊（可稽核：AI 建議 vs 確定性規則）。
    is_injection_day = _is_glp1_injection_day(today)
    is_long_run_day = _is_long_run_today(coach_today)
    rule_allows_heartbeat = (
        verdict is None or verdict.readiness_level in _HEARTBEAT_ALLOWED_LEVELS
    )
    is_heartbeat = (
        rule_allows_heartbeat
        and _is_maintain_verdict(message)
        and not _has_warning_flags(
            is_injection_day, metrics, rpe_niggle_records, is_long_run_day
        )
    )
    if is_heartbeat:
        message = f"✅ 一切正常 — 照手錶練。{_yesterday_task_summary()}"
    elif rule_section:
        message = f"{message}\n\n{rule_section}"

    # ── 3d. 長跑日附當日補給演練目標（T11，確定性計算）──────────────────
    # 長跑日必為 warning 旗標 → 不會走心跳分流，這裡一定是完整建議訊息。
    if is_long_run_day:
        message = f"{message}\n\n{fuel_target_line(today)}"

    # ── 3d2. 明日長跑前瞻（T17）：②段「補給與水分」的前瞻行 ───────────────
    # 只在完整建議訊息才附加（心跳單行維持 T09 的一行約定，不因為「明日」
    # 這種非急迫訊號而被打破）。
    if not is_heartbeat:
        tomorrow_preview = _tomorrow_long_run_preview_line(today)
        if tomorrow_preview:
            message = f"{message}\n\n{tomorrow_preview}"

    # ── 3e. 睡眠一級管理（T18）：完整建議訊息一律附今晚就寢建議 ───────────
    # 心跳單行維持 T09 的「無新訊號→一行」設計不變；只在有完整建議時才
    # 附上就寢建議區段（管理是常態不是懲罰，但不因此打破心跳單行約定）。
    if not is_heartbeat:
        message = f"{message}\n\n{build_sleep_section(health_records, today)}"

    # ── 3f. 系統心跳摘要（T09 心跳一行，T17 固定為完整日報第④段）─────────
    # 心跳單行（is_heartbeat=True）本身已經是「✅ 一切正常...+ 昨日任務摘要」
    # 的壓縮格式（見上方 is_heartbeat 分支），此處只補完整訊息原本缺的第
    # ④「系統」段——四段式日報要求每則完整訊息都看得到任務健康摘要，不是
    # 只有無訊號日才附。
    if not is_heartbeat:
        message = f"{message}\n\n🛠️ 系統：{_yesterday_task_summary()}"

    # ── 4. 推送 ─────────────────────────────────────────────────────────
    if dry_run:
        logger.info("乾跑模式 — 訊息如下：")
        sys.stdout.buffer.write(("\n" + message + "\n").encode("utf-8"))
        return message

    if not send_telegram_text(message):
        append_run("daily_adjust.py", ok=False, wrote_notion=False)
        return None

    # 遵循率只能使用確定已送達的建議，不能包含預覽或失敗的推送。
    _log_advice(today, message, rpe_niggle_records, verdict=verdict, trigger_reason=trigger_reason)
    # daily_adjust 本身不寫 Notion（只讀 Activity DB、推 Telegram），wrote_notion 恆為 False。
    append_run("daily_adjust.py", ok=True, wrote_notion=False)

    return message


# ── RPE / Niggle 訊號 ────────────────────────────────────────────────


def _fetch_recent_rpe_niggle(days: int = 3) -> List[ActivityData]:
    """讀取最近 N 天 Activity DB 中含有 RPE 或 Niggle 回報的紀錄。

    Activity DB 的唯一寫入者 health_tracker_web.py 未排程，資料可能過期或
    當天完全沒有活動列 — 查無資料是常態路徑，不是例外，一律回傳空清單。

    Args:
        days: 回溯天數（預設 3 天）。

    Returns:
        含有 ``rpe`` 或 ``niggle_score`` 的 ActivityData 清單（依日期新到舊）；
        Notion 未設定或查詢失敗時回傳空清單。
    """
    from config import get_activity_api_key

    api_key = get_activity_api_key()  # Activity DB 在獨立 workspace，須用專屬 key（見 config）
    db_id = os.getenv("ACTIVITY_DB_ID", "")
    if not api_key or not db_id:
        logger.info("NOTION_API_KEY/ACTIVITY_DB_ID 未設定，跳過 RPE/Niggle 讀取")
        return []

    try:
        from notion_client_helper import get_notion_client
        from scripts.notion_reader import fetch_activity_history

        client = get_notion_client(api_key)
        records = fetch_activity_history(client, db_id, days=days)
        return [
            r for r in records if r.rpe is not None or r.niggle_score is not None
        ]
    except Exception as exc:
        logger.warning("RPE/Niggle 讀取失敗：%s", exc)
        return []


# ── 睡眠一級管理（T18）───────────────────────────────────────────────


def _fetch_recent_health_records(days: int = 7) -> List[HealthData]:
    """讀取最近 N 天 Health DB 紀錄，供睡眠債計算使用（T18）。

    Health DB 由 health_tracker.py 每日排程被動同步（Garmin 睡眠數據，零使用
    者輸入成本），查無資料（NOTION_API_KEY/HEALTH_DB_ID 未設定、查詢失敗）
    一律回傳空清單 — 呼叫端（``utils.sleep.sleep_debt_7d``、規則引擎）本就
    對缺資料有降級處理，不視為例外。

    Args:
        days: 回溯天數（預設 7，供 7 日睡眠債計算）。

    Returns:
        HealthData 清單（依日期新到舊）；查無資料回傳空清單。
    """
    api_key = os.getenv("NOTION_API_KEY", "")
    db_id = os.getenv("HEALTH_DB_ID", "")
    if not api_key or not db_id:
        logger.info("NOTION_API_KEY/HEALTH_DB_ID 未設定，跳過睡眠債歷史讀取")
        return []

    try:
        from notion_client_helper import get_notion_client
        from scripts.notion_reader import fetch_health_history

        client = get_notion_client(api_key)
        return fetch_health_history(client, db_id, days=days)
    except Exception as exc:
        logger.warning("Health 歷史讀取失敗（睡眠債計算跳過）：%s", exc)
        return []


def _format_rpe_niggle_section(records: List[ActivityData]) -> str:
    """把最近的 RPE/Niggle 回報格式化為 prompt 用的文字區塊。

    Args:
        records: `_fetch_recent_rpe_niggle` 回傳的紀錄清單。

    Returns:
        供 AI prompt 使用的文字區塊；無資料時說明「無回報」而非留白。
    """
    if not records:
        return "最近 3 天 RPE / 痠痛（Niggle）回報：無（可能沒戴錶或還沒回報）"

    lines = ["最近 3 天 RPE / 痠痛（Niggle）回報："]
    has_high_niggle = False
    for r in records:
        day = (r.start_time or "?")[:10]
        parts = []
        if r.rpe is not None:
            parts.append(f"RPE {r.rpe}")
        if r.niggle_score is not None:
            marker = "⚠️" if r.niggle_score >= 3 else ""
            parts.append(f"Niggle {r.niggle_score}{marker}")
            if r.niggle_score >= 3:
                has_high_niggle = True
        if r.notes:
            parts.append(f"備註：{r.notes}")
        lines.append(f"- {day}: {' / '.join(parts)}")

    if has_high_niggle:
        lines.append("⚠️ 近期有 Niggle ≥3 的痠痛回報，建議中必須提及並偏保守。")

    return "\n".join(lines)


def _log_advice(
    today: date,
    message: str,
    rpe_niggle_records: List[ActivityData],
    verdict: Optional[RuleVerdict] = None,
    trigger_reason: str = "",
) -> None:
    """把當日建議附加寫入 ``logs/advice_log.jsonl``，供 T15 weekly_review 事後比對遵循率。

    純附加寫入（append-only），失敗時只記 log，不影響主流程（推送已完成）。

    新增（T15）：加入結構化 ``verdict``（"rest"/"reduce"/"maintain"，優先取自
    規則引擎 T10 的判定；規則引擎不可用時退回從 AI 輸出文字粗略解析，低精度
    可接受）、``key_metric``（觸發依據，供事後稽核）、``message_hash``（訊息
    內容雜湊，供未來去重/比對用）。既有欄位（date/source/message/
    niggle_alert/logged_at）保持不變，維持向後相容。

    Args:
        today: 當日日期。
        message: 已生成（並嘗試推送）的建議文字。
        rpe_niggle_records: 當次用到的 RPE/Niggle 訊號紀錄。
        verdict: 規則引擎判定結果（T10）；None 時退回文字解析。
        trigger_reason: 規則引擎觸發依據（見 ``_rule_trigger_reason``），
            存為 ``key_metric``；``verdict`` 為 None 時忽略此參數。
    """
    try:
        _ADVICE_LOG.parent.mkdir(exist_ok=True)
        if verdict is not None:
            category = _verdict_to_category(verdict)
            key_metric = trigger_reason or None
        else:
            category = _infer_category_from_message(message)
            key_metric = None
        payload = {
            "date": today.isoformat(),
            "source": "daily_adjust",
            "message": message,
            "verdict": category,
            "key_metric": key_metric,
            "message_hash": hashlib.sha256(message.encode("utf-8")).hexdigest()[:12],
            "niggle_alert": any(
                (r.niggle_score or 0) >= 3 for r in rpe_niggle_records
            ),
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(_ADVICE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("advice_log 寫入失敗（忽略）：%s", exc)


# 規則引擎等級 → T15 遵循率計算用的三級 verdict 類別。
_VERDICT_CATEGORY_MAP = {
    "REST": "rest",
    "EASY": "reduce",
    "MODERATE": "maintain",
    "HIGH": "maintain",
}


def _verdict_to_category(verdict: RuleVerdict) -> str:
    """把規則引擎的 ``readiness_level`` 映射成 T15 遵循率用的三級類別。

    Args:
        verdict: 規則引擎判定結果。

    Returns:
        ``"rest"`` / ``"reduce"`` / ``"maintain"``；未知等級時保守回傳
        ``"maintain"``（不會被誤判為需要休息/降載）。
    """
    return _VERDICT_CATEGORY_MAP.get(verdict.readiness_level, "maintain")


def _infer_category_from_message(message: str) -> str:
    """規則引擎判定缺席時（罕見：僅內部錯誤降級），從 AI 輸出文字粗略推斷建議類別。

    低精度可接受（T15 任務要求）：僅用簡單關鍵字比對，不做語意分析。

    Args:
        message: AI 生成（或心跳/覆寫替換後）的最終建議文字。

    Returns:
        ``"rest"`` / ``"reduce"`` / ``"maintain"``。
    """
    if _is_maintain_verdict(message):
        return "maintain"
    if "休息" in message:
        return "rest"
    return "reduce"


# ── Garmin Coach 今日排程 ────────────────────────────────────────────


def _fetch_today_garmin_coach(today: date) -> Optional[dict]:
    """從 Garmin Coach 計劃中取出今天的排程。"""
    plan_id = os.getenv("GARMIN_COACH_PLAN_ID", "")
    if not plan_id:
        logger.info("GARMIN_COACH_PLAN_ID 未設定")
        return None

    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")
    if not email or not password:
        return None

    try:
        from garminconnect import Garmin
        token_dir = str(Path(__file__).resolve().parent.parent / ".garmin_tokens")
        Path(token_dir).mkdir(exist_ok=True)
        client = Garmin(email, password)
        client.login(tokenstore=token_dir)
        detail = client.get_adaptive_training_plan_by_id(int(plan_id))
    except Exception as exc:
        logger.warning("Garmin Coach 讀取失敗：%s", exc)
        return None

    today_iso = today.isoformat()
    for task in detail.get("taskList") or []:
        if task.get("calendarDate") == today_iso:
            tw = task.get("taskWorkout") or {}
            return {
                "workout_name": tw.get("workoutName"),
                "description": tw.get("workoutDescription"),
                "training_effect": tw.get("trainingEffectLabel"),
                "distance_m": tw.get("estimatedDistanceInMeters"),
                "duration_s": tw.get("estimatedDurationInSecs"),
                "is_rest_day": tw.get("restDay", False),
            }
    return None


# ── 最新生理數據 ─────────────────────────────────────────────────────


def _fetch_latest_metrics() -> dict:
    """讀取最新 Training Readiness / Sleep / HRV / Body Battery。

    抓昨天的資料（早上時 Garmin 可能還沒同步今天的數據）。
    """
    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")
    if not email or not password:
        return {}

    try:
        from garminconnect import Garmin
        token_dir = str(Path(__file__).resolve().parent.parent / ".garmin_tokens")
        client = Garmin(email, password)
        client.login(tokenstore=token_dir)

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        metrics = {}

        # Training Readiness
        try:
            tr = client.get_training_readiness(yesterday)
            if tr and isinstance(tr, list) and len(tr) > 0:
                latest = tr[0]
                metrics["readiness_score"] = latest.get("score")
                metrics["readiness_level"] = latest.get("level")
                metrics["hrv_weekly_avg"] = latest.get("hrvWeeklyAverage")
                metrics["hrv_feedback"] = latest.get("hrvFactorFeedback")
                metrics["sleep_history_feedback"] = latest.get("sleepHistoryFactorFeedback")
                metrics["recovery_time_min"] = latest.get("recoveryTime")
                metrics["acute_load"] = latest.get("acuteLoad")
        except Exception as exc:
            logger.warning("Training Readiness 讀取失敗：%s", exc)

        # 昨晚睡眠
        try:
            sleep = client.get_sleep_data(yesterday)
            daily_sleep = sleep.get("dailySleepDTO", {})
            sleep_seconds = daily_sleep.get("sleepTimeSeconds")
            if sleep_seconds:
                metrics["sleep_hours"] = round(sleep_seconds / 3600, 1)
            score = daily_sleep.get("sleepScores", {}).get("overall", {}).get("value")
            if score:
                metrics["sleep_score"] = int(score)
        except Exception as exc:
            logger.warning("睡眠資料讀取失敗：%s", exc)

        # Body Battery
        try:
            bb = client.get_body_battery(yesterday)
            if bb and len(bb) > 0:
                last_entry = bb[-1]
                val = last_entry.get("bodyBattery", {}).get("value")
                if val is not None:
                    metrics["body_battery_end"] = int(val)
        except Exception as exc:
            logger.warning("Body Battery 讀取失敗：%s", exc)

        # Training Status / ACWR
        try:
            ts = client.get_training_status(yesterday)
            mts = ts.get("mostRecentTrainingStatus") or {}
            latest_ts = mts.get("latestTrainingStatusData") or {}
            for device_ts in latest_ts.values():
                acuteDTO = device_ts.get("acuteTrainingLoadDTO") or {}
                metrics["acwr_ratio"] = acuteDTO.get("dailyAcuteChronicWorkloadRatio")
                metrics["acwr_status"] = acuteDTO.get("acwrStatus")
                break
        except Exception as exc:
            logger.warning("Training Status 讀取失敗：%s", exc)

        return metrics

    except Exception as exc:
        logger.error("Garmin 連線失敗：%s", exc)
        return {}


# ── 規則引擎接入（T10）───────────────────────────────────────────────


def _load_athlete_profile() -> AthleteProfile:
    """組出規則引擎與 AI prompt 共用的運動員檔案。

    GLP-1 相關欄位從環境設定讀取（單一來源 ``get_glp1_settings``）。VDOT
    優先讀取 ``.env`` 的 ``VDOT``（單一來源 ``config.get_vdot``），未設定
    時退回 ``AthleteProfile`` 的示範預設值。部署前需確認私人設定。

    Returns:
        規則引擎評估用的 AthleteProfile。
    """
    glp1 = get_glp1_settings()
    return AthleteProfile(
        vdot=get_vdot(),
        glp1_injection_day=glp1.injection_day,
        glp1_severity=glp1.severity,
    )


def _metrics_to_health_data(metrics: dict) -> HealthData:
    """把 ``_fetch_latest_metrics`` 的 dict 轉成規則引擎需要的 ``HealthData``。

    欄位對應說明：
    - ``body_battery`` ← ``body_battery_end``（昨晚結束值）。
    - ``training_readiness`` ← ``readiness_score``。
    - ``recovery_time``：metrics 存分鐘（``recovery_time_min``），HealthData
      約定為小時，這裡換算（四捨五入）。
    - ``hrv_last_night``：daily_adjust 的 Garmin 讀取路徑沒有這個值 → None，
      規則引擎會自動跳過 HRV ratio 檢查（Rule 5/8 走「正常 HRV」分支）。

    Args:
        metrics: ``_fetch_latest_metrics`` 回傳的生理數據 dict。

    Returns:
        規則引擎評估用的 HealthData（缺值一律為 None，引擎自行降級）。
    """
    recovery_min = metrics.get("recovery_time_min")
    recovery_hours = (
        int(round(recovery_min / 60.0)) if recovery_min is not None else None
    )
    return HealthData(
        sleep_hours=metrics.get("sleep_hours"),
        sleep_score=metrics.get("sleep_score"),
        body_battery=metrics.get("body_battery_end"),
        training_readiness=metrics.get("readiness_score"),
        training_readiness_level=metrics.get("readiness_level"),
        hrv_weekly_avg=metrics.get("hrv_weekly_avg"),
        recovery_time=recovery_hours,
        acute_load=metrics.get("acute_load"),
    )


def _derive_chronic_load(metrics: dict) -> Optional[float]:
    """從 Garmin 回報的 ACWR 與 acute load 反推 chronic load。

    規則引擎的 ACWR 檢查需要 ``chronic_load``（daily_plan 過去從 Notion 28 天
    歷史計算）。daily_adjust 直接拿 Garmin 的 ``acwr_ratio`` 反推
    （chronic = acute / acwr），讓引擎算回同一個 ACWR 值。

    Args:
        metrics: ``_fetch_latest_metrics`` 回傳的生理數據 dict。

    Returns:
        反推的 chronic load；任一數據缺失或非法時回傳 None
        （規則引擎會跳過 ACWR 檢查並標記 ``insufficient_history``）。
    """
    acute = metrics.get("acute_load")
    acwr = metrics.get("acwr_ratio")
    if acute is None or acwr is None:
        return None
    try:
        acwr_f = float(acwr)
        if acwr_f <= 0:
            return None
        return float(acute) / acwr_f
    except (TypeError, ValueError):
        return None


def _evaluate_rule_verdict(
    today: date, metrics: dict, sleep_debt_7d: Optional[float] = None
) -> Optional[RuleVerdict]:
    """執行規則引擎，取得今日確定性判定（9 條規則）。

    引擎本身是純函式（無外部 API），理論上不會失敗；此處仍包一層
    try/except，萬一內部錯誤時降級為「無規則模式」（回傳 None，當日建議
    退回純 AI 行為），不讓晨間訊息整個死掉。

    Args:
        today: 今天日期（GLP-1 窗口判斷用）。
        metrics: ``_fetch_latest_metrics`` 回傳的生理數據 dict。
        sleep_debt_7d: 近 7 天累積睡眠債（見 ``utils.sleep.sleep_debt_7d``），
            T18 睡眠優先規則用；None 時該規則只保留「昨晚 < 4.5h」判斷。

    Returns:
        規則引擎判定結果；引擎內部錯誤時回傳 None。
    """
    try:
        from scripts.rule_engine import evaluate

        return evaluate(
            health=_metrics_to_health_data(metrics),
            profile=_load_athlete_profile(),
            config=_RULE_CONFIG,
            today=today,
            chronic_load=_derive_chronic_load(metrics),
            sleep_debt_7d=sleep_debt_7d,
        )
    except Exception as exc:
        logger.error("規則引擎評估失敗（本次降級為純 AI 模式）：%s", exc)
        return None


def _rule_trigger_reason(
    verdict: RuleVerdict,
    health: HealthData,
    config: RuleConfig,
    sleep_debt_7d: Optional[float] = None,
) -> str:
    """組出「哪條規則、用什麼數據與閾值」的一行說明。

    閾值一律從 ``RuleConfig`` 內插，不寫死在模板（T10 要求）；
    例：``Body Battery 18 < 30``。

    Args:
        verdict: 規則引擎判定結果（依 flags 判斷觸發規則）。
        health: 餵給引擎的健康數據（取實際數值）。
        config: 規則引擎閾值設定（取門檻數值）。
        sleep_debt_7d: 近 7 天累積睡眠債（T18），供「睡眠優先」規則的說明
            文字引用；未提供時退回昨晚睡眠時數的說明。

    Returns:
        一行觸發依據說明文字。
    """
    if "sleep_debt" in verdict.flags:
        if sleep_debt_7d is not None and sleep_debt_7d > config.sleep_debt_downgrade_hours:
            return f"睡眠優先：本週睡眠債 {sleep_debt_7d:.1f}h > {config.sleep_debt_downgrade_hours}h"
        return f"睡眠優先：昨晚 {health.sleep_hours}h < {config.sleep_acute_downgrade_hours}h"
    if "low_body_battery" in verdict.flags:
        return f"Body Battery {health.body_battery} < {config.body_battery_rest}"
    if "low_readiness" in verdict.flags:
        return (
            f"Training Readiness {health.training_readiness} < {config.readiness_rest}"
        )
    if "sleep_deficit" in verdict.flags:
        return f"睡眠 {health.sleep_hours}h < {config.sleep_downgrade_hours}h"
    if "glp1_window" in verdict.flags:
        return f"GLP-1 注射後 {config.glp1_cooldown_hours}h 冷卻窗口內"
    if "no_readiness_data" in verdict.flags:
        return "Training Readiness 無資料 → 保守預設 MODERATE"
    if health.training_readiness is not None:
        return (
            f"Training Readiness {health.training_readiness}"
            f"（休息門檻 {config.readiness_rest}／高強度門檻 {config.readiness_moderate}）"
            "+ HRV 組合"
        )
    return "綜合判定"


def _format_rule_verdict_section(verdict: RuleVerdict, trigger_reason: str) -> str:
    """把規則引擎判定格式化為結構化文字區塊。

    同一個區塊有兩個用途：注入 AI prompt（結構化輸入），以及附在最終
    訊息尾端（讓使用者可稽核 AI 建議 vs 確定性規則）。

    Args:
        verdict: 規則引擎判定結果。
        trigger_reason: ``_rule_trigger_reason`` 產出的觸發依據。

    Returns:
        多行文字區塊，第一行固定為「🧭 規則引擎判定…」。
    """
    lines = [
        "🧭 規則引擎判定（確定性規則，非 AI）：",
        f"- 等級：{verdict.readiness_level} — {verdict.recommended_intensity}"
        f"（最高心率區間 Z{verdict.max_hr_zone}）",
        f"- 依據：{trigger_reason}",
    ]
    for warning in verdict.warnings:
        lines.append(f"- ⚠️ {warning}")
    return "\n".join(lines)


def _enforce_rule_verdict(
    message: str,
    verdict: RuleVerdict,
    trigger_reason: str,
) -> Tuple[str, bool]:
    """程式層防線：規則判定 REST/EASY 時，攔下仍建議高強度的 AI 輸出。

    比對範圍優先取「**結論**」之後的文字（避免「今日排程」段落引用
    Garmin 原排程名稱造成誤判）；找不到結論標記時退回比對全文。
    判定 REST 時額外要求結論必須出現「休息」字樣 — REST 下連輕鬆跑
    都不該出現。刻意用簡單關鍵字比對（任務要求：不要過度工程）。

    Args:
        message: AI 生成的完整建議訊息。
        verdict: 規則引擎判定結果。
        trigger_reason: 觸發依據（覆寫訊息中引用）。

    Returns:
        ``(最終訊息, 是否覆寫)``；未覆寫時原樣回傳。
    """
    if verdict.readiness_level not in ("REST", "EASY"):
        return message, False

    marker = "**結論**"
    conclusion = message[message.index(marker):] if marker in message else message
    conclusion_lower = conclusion.lower()

    violated = any(kw in conclusion_lower for kw in _HIGH_INTENSITY_KEYWORDS)
    if not violated and verdict.readiness_level == "REST" and "休息" not in conclusion:
        violated = True

    if not violated:
        return message, False

    override = "\n".join(
        [
            "🛑 **規則引擎覆寫**（AI 建議強度高於確定性規則上限，以規則為準）",
            f"**結論**：⚠️ 調整為 {verdict.recommended_intensity}"
            f"（最高心率區間 Z{verdict.max_hr_zone}）",
            f"**原因**：規則引擎判定 {verdict.readiness_level}（{trigger_reason}）",
        ]
    )
    return override, True


# ── 長跑補給演練（T11）────────────────────────────────────────────────


def _is_long_run_today(coach: Optional[dict]) -> bool:
    """判斷今日 Garmin Coach 排程是否為長跑（補給演練適用日）。

    長跑定義由 ``utils.fueling.is_long_run`` 單一來源判定（時間 ≥90 分鐘，
    或無時間估計時距離 ≥14km）。Garmin Coach 不可用（None）或今日為休息日
    時一律回傳 False — 資料缺失時寧可不發補給提醒，也不要天天誤報。

    Args:
        coach: ``_fetch_today_garmin_coach`` 回傳的今日排程 dict（可為 None）。

    Returns:
        今日是否為長跑日。
    """
    if not coach or coach.get("is_rest_day"):
        return False

    duration_s = coach.get("duration_s")
    distance_m = coach.get("distance_m")
    duration_min = duration_s / 60.0 if duration_s else None
    distance_km = distance_m / 1000.0 if distance_m else None
    return is_long_run(duration_min, distance_km)


# ── 明日長跑前瞻（T17）：晨間日報②段的「今晚先備妥補給」提示 ─────────────

# 手動抄錄的 coach_week.json 沒有距離/時間欄位，額外用型態關鍵字補強判斷
# （使用者可能沒在 target 欄寫數字，只寫了 "Long Run"）。
_LONG_RUN_NAME_KEYWORDS = ("long run", "長跑", "lsd")


def _coach_week_task_is_long_run(task: "GarminCoachTask") -> bool:
    """判斷 coach_week.json 單日排程是否為長跑。

    手動抄錄的排程（見 ``scripts.training_advisor._load_manual_coach_week``）
    沒有結構化的 ``estimated_distance_m`` / ``estimated_duration_s``（一律
    為 None），因此改從 ``workout_description``（使用者抄錄的 target 文字，
    例如「18km @ 7:00/km」）用 ``utils.fueling.parse_workout_text`` 擷取距離
    /時間後，交給 ``is_long_run`` 判斷（與今日長跑判定同一套邏輯，見
    ``_is_long_run_today``）；抓不到數字時，退而用 ``workout_name``/型態關鍵
    字（"Long Run"/"長跑"/"LSD"）補強。

    Args:
        task: coach_week.json 轉出的單日排程（``GarminCoachTask``）。

    Returns:
        是否判定為長跑（明日補給前瞻適用）。
    """
    if task.is_rest_day:
        return False

    duration_min, distance_km = parse_workout_text(task.workout_description)
    if is_long_run(duration_min, distance_km):
        return True

    name = (task.workout_name or "").lower()
    return any(kw in name for kw in _LONG_RUN_NAME_KEYWORDS)


def _tomorrow_long_run_preview_line(today: Optional[date] = None) -> Optional[str]:
    """若明日（依 coach_week.json）為長跑，組出今晚碳水裝載前瞻提示行（T17）。

    來源刻意選 T12 的 coach_week.json 手動課表，而非再呼叫一次 Garmin Coach
    API 查「明天」——``_fetch_today_garmin_coach`` 本就只查「今天」，為了查
    明天再登入一次 Garmin 太脆弱（API 本身就常被 429 擋）；coach_week.json
    本身就是完整一週的本地檔案，零額外 API 成本，剛好可以看到「明天」。

    檔案缺失/過期/查無明日排程/明日非長跑，一律回傳 None（略過此行，不猜測
    ——寧可漏推一次前瞻提醒，也不要瞎編一個不存在的長跑日）。

    Args:
        today: 基準日（預設今天）。

    Returns:
        一行前瞻提示文字；條件不成立或 coach_week.json 不可用時回傳 None。
    """
    today = today or date.today()
    tomorrow = today + timedelta(days=1)

    try:
        from scripts.training_advisor import _load_manual_coach_week
        coach = _load_manual_coach_week()
    except Exception as exc:
        logger.warning("coach_week.json 讀取失敗（略過明日長跑前瞻）：%s", exc)
        return None

    if coach is None:
        return None

    tomorrow_iso = tomorrow.isoformat()
    task = next((t for t in coach.tasks if t.calendar_date == tomorrow_iso), None)
    if task is None or not _coach_week_task_is_long_run(task):
        return None

    weeks_left = weeks_to_race(tomorrow)
    lo, hi = carb_target_g_per_hr(weeks_left)
    return (
        f"📅 明日長跑（依 coach_week.json，距賽 {weeks_left} 週）："
        f"今晚先備妥補給，明日碳水裝載目標每小時 {lo}-{hi}g"
    )


# ── 心跳分流輔助（T09）────────────────────────────────────────────────


def _is_glp1_injection_day(today: date) -> bool:
    """判斷指定日期是否為 GLP-1 注射日（單一來源，供 prompt 組裝與心跳分流共用）。

    Args:
        today: 要判斷的日期。

    Returns:
        當天的星期幾與設定的注射日相符則為 True（不論嚴重度，
        現行 ``_generate_daily_adjustment`` 的行為向來如此——嚴重度只決定
        AI 要不要因此調整計劃，不影響是否顯示注射日提醒）。
    """
    return today.weekday() == get_glp1_settings().injection_day


def _is_maintain_verdict(message: str) -> bool:
    """判斷 AI 微調建議的結論是否為「維持原計劃」。

    依系統提示（見 ``_generate_daily_adjustment``）固定格式，結論行只會是
    「✅ 維持」或「⚠️ 建議調整為 X」兩者之一。抓不到明確的「維持」標記時
    （格式跑掉、AI 輸出不符預期等）一律保守回傳 False——寧可多發一次完整
    建議，也不要因為誤判而把真正的調整訊號壓成一行心跳。

    Args:
        message: ``_generate_daily_adjustment`` 回傳的完整訊息文字。

    Returns:
        結論明確為「維持」時回傳 True。
    """
    return "✅ 維持" in message and "建議調整" not in message


def _has_warning_flags(
    is_injection_day: bool,
    metrics: dict,
    rpe_niggle_records: List[ActivityData],
    is_long_run_day: bool = False,
) -> bool:
    """判斷是否有任何需要使用者注意的 warning 旗標。

    涵蓋 GLP-1 注射日、Body Battery 過低、Niggle（痠痛前兆）≥3、
    長跑補給演練日（T11：當日訊息必須附補給目標，不可壓成一行心跳）
    四種訊號來源。

    Args:
        is_injection_day: 今天是否為 GLP-1 注射日（見 ``_is_glp1_injection_day``）。
        metrics: 昨晚/今早生理數據（見 ``_fetch_latest_metrics``）。
        rpe_niggle_records: 最近幾天的 RPE/Niggle 回報。
        is_long_run_day: 今天是否為長跑日（見 ``_is_long_run_today``）。

    Returns:
        任一 warning 旗標成立時回傳 True。
    """
    if is_injection_day:
        return True

    if is_long_run_day:
        return True

    body_battery = metrics.get("body_battery_end")
    if body_battery is not None and body_battery < _LOW_BODY_BATTERY_THRESHOLD:
        return True

    if any((r.niggle_score or 0) >= 3 for r in rpe_niggle_records):
        return True

    return False


def _yesterday_task_summary() -> str:
    """統計昨日 run_ledger 中各排程的成功/總數，供心跳訊息附註任務健康摘要。

    Returns:
        形如 ``"昨日任務 3/3 成功"`` 的一行文字；``run_ledger.jsonl`` 查無
        昨日紀錄時回傳 ``"昨日任務：無紀錄"``。``read_recent_runs`` 本身已
        內建錯誤處理（檔案不存在/解析失敗都回傳空清單），不會拋出例外。
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    records = [r for r in read_recent_runs() if r.get("date") == yesterday]
    if not records:
        return "昨日任務：無紀錄"
    ok_count = sum(1 for r in records if r.get("ok"))
    return f"昨日任務 {ok_count}/{len(records)} 成功"


# ── AI 微調建議 ──────────────────────────────────────────────────────


def _generate_daily_adjustment(
    today: date,
    coach: Optional[dict],
    metrics: dict,
    rpe_niggle_records: Optional[List[ActivityData]] = None,
    verdict: Optional[RuleVerdict] = None,
    rule_section: Optional[str] = None,
    resting_hr: Optional[float] = None,
) -> Optional[str]:
    """呼叫 AI 生成今日微調建議。

    Args:
        today: 當日日期。
        coach: Garmin Coach 今日排程（見 ``_fetch_today_garmin_coach``）。
        metrics: 昨晚/今早生理數據（見 ``_fetch_latest_metrics``）。
        rpe_niggle_records: 最近 3 天 RPE/Niggle 回報
            （見 ``_fetch_recent_rpe_niggle``），預設為 None（視為空清單）。
        verdict: 規則引擎判定（T10）；REST/EASY 時系統提示加入硬性上限，
            None 時維持純 AI 行為。
        rule_section: 規則引擎判定的結構化文字區塊
            （見 ``_format_rule_verdict_section``），會注入 user message。
        resting_hr: 安靜心率（bpm），供 Karvonen 心率區間計算用（T13）。
            daily_adjust 的既有資料流（``_fetch_latest_metrics``）沒有讀
            Health DB 歷史，預設 None 時退回
            ``utils.vdot_paces.DEFAULT_RESTING_HR``；保留此參數供未來接上
            即時滾動均值使用。
    """
    from scripts.ai_coach import _call_ai_api
    from utils.vdot_paces import DEFAULT_RESTING_HR, format_pace, karvonen_zones, vdot_to_paces

    rpe_niggle_records = rpe_niggle_records or []

    # ── 配速表與心率區間（T13）：VDOT 動態推導，取代寫死數字 ──────────
    profile = _load_athlete_profile()
    rhr = resting_hr if resting_hr is not None else DEFAULT_RESTING_HR
    paces = vdot_to_paces(profile.vdot)
    hr_zones = karvonen_zones(profile.max_hr, rhr)
    pace_zone_section = (
        f"跑者配速表（VDOT {profile.vdot}）：E {format_pace(paces.easy_min)}-{format_pace(paces.easy_max)} "
        f"/ M {format_pace(paces.marathon)} / T {format_pace(paces.threshold)} "
        f"/ I {format_pace(paces.interval)} / R {format_pace(paces.repetition)} min/km\n"
        f"心率區間（Karvonen，安靜心率 {rhr}bpm）："
        f"Z1<{hr_zones.z1_max} / Z2 {hr_zones.z1_max}-{hr_zones.z2_max} "
        f"/ Z3 {hr_zones.z2_max}-{hr_zones.z3_max} / Z4 {hr_zones.z3_max}-{hr_zones.z4_max} "
        f"/ Z5>{hr_zones.z4_max} bpm"
    )

    weekday = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][today.weekday()]
    today_str = f"{today.isoformat()} {weekday}"

    # ── 示範決策時態（T17）：週間晚上、週末早上，部署前須確認作息 ────
    # daily_adjust 過去統一用「今天早上要不要練」的語氣，週間其實是誤導
    # （決策發生在傍晚）。時態只看星期幾判斷，不靠 AI 自行推斷。
    is_weekend = today.weekday() >= 5  # 5=週六, 6=週日
    tense_label = "今早" if is_weekend else "今晚"
    tense_hint = (
        "今天是週末，訓練在今早進行"
        if is_weekend
        else "今天是平日，訓練在今晚（傍晚後）進行"
    )

    # ── GLP-1 嚴重度 ─────────────────────────────────────────────
    glp1_settings = get_glp1_settings()
    glp1_severity = glp1_settings.severity
    is_injection_day = _is_glp1_injection_day(today)

    glp1_note = {
        "minimal": "幾乎無副作用",
        "mild": "輕微副作用",
        "moderate": "中等副作用",
        "severe": "強烈副作用",
    }.get(glp1_severity, "輕微副作用")

    # ── 組 prompt ──────────────────────────────────────────────────
    coach_section = "今日無 Garmin Coach 排程"
    if coach:
        if coach.get("is_rest_day"):
            coach_section = "今日 Garmin Coach 排程：😴 休息日"
        elif coach.get("workout_name"):
            dist = f" {coach['distance_m']/1000:.1f}km" if coach.get("distance_m") else ""
            dur = f" {coach['duration_s']/60:.0f}min" if coach.get("duration_s") else ""
            hr = f" ({coach['description']})" if coach.get("description") else ""
            coach_section = (
                f"今日 Garmin Coach 排程：{coach['workout_name']}{dist}{dur}{hr}"
                f" [{coach.get('training_effect') or '?'}]"
            )

    metrics_section_lines = ["昨晚 / 今早生理數據："]
    if metrics.get("sleep_hours"):
        sleep_score_str = f" (分數 {metrics.get('sleep_score', '?')})" if metrics.get("sleep_score") else ""
        metrics_section_lines.append(f"- 睡眠：{metrics['sleep_hours']} 小時{sleep_score_str}")
    if metrics.get("readiness_score") is not None:
        metrics_section_lines.append(
            f"- Training Readiness：{metrics['readiness_score']}/100 ({metrics.get('readiness_level', '?')})"
        )
    if metrics.get("body_battery_end") is not None:
        metrics_section_lines.append(f"- Body Battery（昨晚結束）：{metrics['body_battery_end']}")
    if metrics.get("hrv_weekly_avg") is not None:
        metrics_section_lines.append(
            f"- HRV 週均值：{metrics['hrv_weekly_avg']}ms ({metrics.get('hrv_feedback', '?')})"
        )
    if metrics.get("sleep_history_feedback"):
        metrics_section_lines.append(f"- 睡眠歷史趨勢：{metrics['sleep_history_feedback']}")
    if metrics.get("recovery_time_min") is not None:
        metrics_section_lines.append(f"- 恢復時間：{metrics['recovery_time_min']} 分鐘")
    if metrics.get("acwr_ratio") is not None:
        metrics_section_lines.append(
            f"- ACWR：{metrics['acwr_ratio']} ({metrics.get('acwr_status', '?')})"
        )

    if len(metrics_section_lines) == 1:
        metrics_section_lines.append("- ⚠️ 無生理數據（可能未戴錶）")

    metrics_section = "\n".join(metrics_section_lines)

    glp1_section = ""
    if is_injection_day:
        glp1_section = f"\n⚠️ **今天是 GLP-1 注射日**（嚴重度：{glp1_note}）"

    rpe_niggle_section = _format_rpe_niggle_section(rpe_niggle_records)
    has_high_niggle = any((r.niggle_score or 0) >= 3 for r in rpe_niggle_records)

    niggle_rule = (
        "- Niggle（痠痛前兆）≥3 → **必須**在「結論」或「注意」中明確提及此訊號，"
        "並偏保守判斷（肌肉骨骼傷害需要額外注意，HRV/Body Battery "
        "偵測不到這種訊號，主觀痠痛回報是最後一道防線）\n"
    )

    rule_constraint = ""
    if verdict is not None and verdict.readiness_level in ("REST", "EASY"):
        rule_constraint = (
            "\n**規則引擎硬性上限**：使用者訊息中的「規則引擎判定」區塊是"
            f"確定性規則的結果（本次判定：{verdict.readiness_level}），"
            "你的結論強度不得超過該判定 — REST=必須建議休息；EASY=最高只能輕鬆跑。"
            "可以比它更保守，不能更激進。\n"
        )

    system_prompt = (
        "你是專業馬拉松訓練教練。每天早晨，根據跑者昨晚的生理數據，"
        "判斷今天的 Garmin Coach 排程要不要調整。\n\n"
        "**重要原則：**\n"
        "1. 預設「維持原計劃」，只在數據明確顯示問題時才改\n"
        "2. 不要過度保守，跑者要進步就需要適當挑戰\n"
        "3. 缺資料時不要降強度，維持原計劃\n"
        "4. GLP-1 注射依跑者個人嚴重度判斷（minimal=不用調整）\n\n"
        "**修正時機（必須有明確證據）：**\n"
        "- 睡眠 < 5 小時 OR 睡眠分數 < 50 → 降強度\n"
        "- Body Battery < 30 → 改休息或大幅降強度\n"
        "- Training Readiness < 40 → 降強度或休息\n"
        "- HRV feedback = LOW + 睡眠差 → 降強度\n"
        "- ACWR > 1.5 → 過度訓練，降量\n"
        f"{niggle_rule}"
        f"{rule_constraint}\n"
        "**⚠️ 必須引用具體數據！** 每個結論和建議都要有對應的數字佐證。\n"
        "禁止說「狀態良好」「身體 OK」這種空話 — 必須說「Readiness 75（HIGH）+ 睡眠 7.2h（分數 82）+ HRV 52ms（GOOD）」。\n\n"
        "**輸出格式（繁體中文，用 ** 雙星號 標粗體）：**\n"
        "**今日排程**：列出 Garmin Coach 排的具體訓練（含距離、時間、目標心率）\n"
        "**生理數據**：條列引用今天的關鍵數據（睡眠 X.Xh / Readiness XX / Body Battery XX / HRV XXms / ACWR X.X）\n"
        f"**結論**：開頭標明時態「{tense_label}」（{tense_hint}），"
        f"格式如「{tense_label}：節奏跑 6km — 維持」；✅ 維持 OR ⚠️ 建議調整為 X"
        "（要寫具體新建議：如「改 4km @ 130bpm」）\n"
        "**原因**：對照數據說明（例如「Readiness 75 屬 HIGH 範圍，可正常執行」）\n"
        "**注意**：（可選）營養/補水/裝備提醒、Niggle 訊號提醒\n\n"
        "全部不超過 250 字。不要用 HTML 標籤，只用 ** 雙星號 標粗體。"
    )

    rule_block = f"\n{rule_section}\n" if rule_section else ""

    user_message = (
        f"日期：{today_str}\n\n"
        f"{coach_section}\n\n"
        f"{pace_zone_section}\n\n"
        f"{metrics_section}\n\n"
        f"{rpe_niggle_section}\n"
        f"{rule_block}"
        f"{glp1_section}"
    )

    if has_high_niggle:
        user_message += "\n\n⚠️ 系統提醒：近期有 Niggle ≥3 回報，請在建議中明確提及。"

    response = _call_ai_api(
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=512,
    )

    if response is None:
        return None

    # 時態標籤直接寫進標題（T17）：不靠 AI 自行判斷正確，標題本身就是保底
    # 依據——即使 AI 結論忘了標時態，使用者看標題也能知道是今晚還是今早。
    header = f"☀️ **{today_str}｜{tense_label}訓練日報**\n{'─' * 30}\n"
    return header + response


# ── CLI 進入點 ─────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="每日訓練微調建議")
    parser.add_argument("--dry-run", action="store_true", help="只印出，不推送 Telegram")
    args = parser.parse_args()

    result = run_daily_adjust(dry_run=args.dry_run)
    if result is None:
        logger.error("生成失敗")
        sys.exit(1)


if __name__ == "__main__":
    main()
