"""scripts/ai_coach.py — AI 訓練教練（支援 Anthropic / OpenRouter）。

將 Garmin 數據、規則引擎判定、歷史上下文送給 AI，
取得結構化的每日訓練處方或每週回顧。

支援兩種 AI 後端（透過 .env 切換）：
    AI_PROVIDER=anthropic   → 直接呼叫 Anthropic API（預設）
    AI_PROVIDER=openrouter  → 透過 OpenRouter 呼叫各種模型

使用方式::

    from scripts.ai_coach import get_daily_prescription, get_weekly_review

    prescription = get_daily_prescription(health, verdict, context, profile)
    review = get_weekly_review(context, profile)
"""

import json
import sys
import time

import requests
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import (
    AthleteProfile,
    HealthData,
    RuleVerdict,
    TrainingPrescription,
    WeeklyContext,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_DAY_NAMES = {0: "週一", 1: "週二", 2: "週三", 3: "週四", 4: "週五", 5: "週六", 6: "週日"}


# ── 公開介面 ─────────────────────────────────────────────────────────────────


def get_daily_prescription(
    health: HealthData,
    verdict: RuleVerdict,
    context: WeeklyContext,
    profile: AthleteProfile,
    target_date: Optional[date] = None,
    api_key: Optional[str] = None,
) -> TrainingPrescription:
    """取得今日訓練處方。

    Args:
        health: 今日 Garmin 健康數據。
        verdict: 規則引擎判定。
        context: 過去一段時間的訓練上下文。
        profile: 運動員個人檔案。
        target_date: 目標日期（預設今天）。
        api_key: Anthropic API Key（預設從環境變數讀取）。

    Returns:
        TrainingPrescription 結構化處方。若 API 失敗，回傳 fallback 處方。
    """
    if target_date is None:
        target_date = date.today()

    system_prompt = _build_system_prompt(profile, resting_hr=context.avg_resting_hr)
    user_message = _build_daily_message(health, verdict, context, target_date)

    raw_response = _call_ai_api(
        system_prompt=system_prompt,
        user_message=user_message,
        api_key=api_key,
    )

    if raw_response is None:
        logger.warning("AI API 失敗，使用 fallback 處方")
        return _build_fallback_prescription(verdict, target_date)

    prescription = _parse_prescription(raw_response, target_date)
    if prescription is None:
        logger.warning("Claude 回應解析失敗，使用 fallback 處方")
        return _build_fallback_prescription(verdict, target_date)

    return prescription


def get_weekly_review(
    context: WeeklyContext,
    profile: AthleteProfile,
    api_key: Optional[str] = None,
) -> str:
    """取得每週訓練回顧。

    Args:
        context: 過去一週的訓練上下文。
        profile: 運動員個人檔案。
        api_key: Anthropic API Key。

    Returns:
        每週回顧文字。若 API 失敗，回傳簡易摘要。
    """
    system_prompt = _build_system_prompt(profile, resting_hr=context.avg_resting_hr)
    weekly_prompt = _load_prompt("weekly_review_prompt.md")
    weekly_data = _format_weekly_data(context)
    user_message = weekly_prompt.replace("{weekly_data}", weekly_data)

    raw_response = _call_ai_api(
        system_prompt=system_prompt,
        user_message=user_message,
        api_key=api_key,
    )

    if raw_response is None:
        return _build_fallback_weekly_review(context)

    return raw_response


# ── 統一 AI API 介面 ─────────────────────────────────────────────────────────


def _call_ai_api(
    system_prompt: str,
    user_message: str,
    api_key: Optional[str] = None,
    max_tokens: int = 2048,
    max_retries: int = 3,
) -> Optional[str]:
    """統一 AI API 呼叫介面，根據 AI_PROVIDER 環境變數選擇後端。

    環境變數設定：
        AI_PROVIDER        : "anthropic"（預設）或 "openrouter"
        ANTHROPIC_API_KEY  : Anthropic API Key
        ANTHROPIC_MODEL    : Claude 模型（預設 claude-haiku-4-5）
        OPENROUTER_API_KEY : OpenRouter API Key
        OPENROUTER_MODEL   : 模型 ID（預設 google/gemini-2.5-flash）

    Args:
        system_prompt: 系統提示。
        user_message: 使用者訊息。
        api_key: 覆蓋環境變數的 API Key（可選）。
        max_tokens: 最大回應 token 數。
        max_retries: 最大重試次數。

    Returns:
        AI 的文字回應，失敗時回傳 None。
    """
    import os

    provider = os.getenv("AI_PROVIDER", "anthropic").lower().strip()

    if provider == "openrouter":
        key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
        logger.info("使用 OpenRouter（%s）", model)
        return _call_openrouter_api(
            system_prompt=system_prompt,
            user_message=user_message,
            api_key=key,
            model=model,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )

    # 預設：anthropic
    key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    logger.info("使用 Anthropic（%s）", model)
    return _call_claude_api(
        system_prompt=system_prompt,
        user_message=user_message,
        api_key=key,
        model=model,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )


def _call_openrouter_api(
    system_prompt: str,
    user_message: str,
    api_key: Optional[str] = None,
    model: str = "google/gemini-2.5-flash",
    max_tokens: int = 2048,
    max_retries: int = 3,
) -> Optional[str]:
    """呼叫 OpenRouter API（OpenAI 相容格式），支援 Gemini 等多種模型。

    Args:
        system_prompt: 系統提示。
        user_message: 使用者訊息。
        api_key: OpenRouter API Key（預設從 OPENROUTER_API_KEY 讀取）。
        model: 模型 ID，如 "google/gemini-2.5-flash"。
        max_tokens: 最大回應 token 數。
        max_retries: 最大重試次數。

    Returns:
        模型的文字回應，失敗時回傳 None。
    """
    import os

    if api_key is None:
        api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.error("OPENROUTER_API_KEY 未設定")
        return None

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/life-os",
        "X-Title": "LifeOS Training Advisor",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)

            if response.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "OpenRouter rate limit，等待 %ds（%d/%d）",
                    wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
                continue

            if response.status_code != 200:
                logger.error(
                    "OpenRouter API 錯誤 HTTP %d：%s",
                    response.status_code,
                    response.text[:300],
                )
                return None

            data = response.json()
            text = data["choices"][0]["message"]["content"]
            logger.info("OpenRouter 回應成功（%s）", model)
            return text

        except requests.exceptions.Timeout:
            logger.warning("OpenRouter 請求超時（%d/%d）", attempt + 1, max_retries)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

        except Exception as exc:
            logger.error("OpenRouter 非預期錯誤：%s", exc)
            return None

    return None


# ── Claude API 呼叫 ─────────────────────────────────────────────────────────


def _call_claude_api(
    system_prompt: str,
    user_message: str,
    api_key: Optional[str] = None,
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 2048,
    max_retries: int = 3,
) -> Optional[str]:
    """呼叫 Claude API，帶指數退避重試。

    Args:
        system_prompt: 系統提示。
        user_message: 使用者訊息。
        api_key: API Key（預設從環境變數讀取）。
        model: 使用的模型。
        max_tokens: 最大回應 token 數。
        max_retries: 最大重試次數。

    Returns:
        Claude 的文字回應，失敗時回傳 None。
    """
    import os

    if api_key is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY 未設定")
        return None

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic 套件未安裝，請執行: pip install anthropic")
        return None

    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text
            logger.info(
                "Claude API 回應成功（%d tokens）",
                response.usage.output_tokens,
            )
            return text

        except anthropic.RateLimitError:
            wait = 2 ** (attempt + 1)
            logger.warning("Rate limit，等待 %ds 後重試（%d/%d）", wait, attempt + 1, max_retries)
            time.sleep(wait)

        except anthropic.APIError as exc:
            logger.error("Claude API 錯誤：%s", exc)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return None

        except Exception as exc:
            logger.error("非預期錯誤：%s", exc)
            return None

    return None


# ── Prompt 建構 ──────────────────────────────────────────────────────────────


def _load_prompt(filename: str) -> str:
    """讀取 prompts 目錄下的 prompt 模板。"""
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def _build_system_prompt(
    profile: AthleteProfile,
    resting_hr: Optional[float] = None,
) -> str:
    """用運動員檔案填充 system prompt 模板。

    配速表（E/M/T/I/R）與心率區間邊界皆由 VDOT／(max_hr, resting_hr)
    現算（``utils.vdot_paces``），不再是寫死在模板裡的數字（T13）。

    Args:
        profile: 運動員檔案。
        resting_hr: 安靜心率（bpm），建議傳入近期滾動實測均值（例如
            ``WeeklyContext.avg_resting_hr``）；None 或無法取得時退回
            ``utils.vdot_paces.DEFAULT_RESTING_HR``。

    Returns:
        填充後的完整 system prompt 文字。
    """
    from utils.vdot_paces import DEFAULT_RESTING_HR, karvonen_zones, vdot_to_paces

    template = _load_prompt("system_prompt.md")
    paces = vdot_to_paces(profile.vdot)
    rhr = resting_hr if resting_hr is not None else DEFAULT_RESTING_HR
    zones = karvonen_zones(profile.max_hr, rhr)

    return template.format(
        weight_kg=profile.weight_kg,
        target_weight_kg=profile.target_weight_kg,
        vdot=profile.vdot,
        max_hr=profile.max_hr,
        injection_day_name=_DAY_NAMES.get(profile.glp1_injection_day, "未知"),
        easy_pace_min=_format_pace(paces.easy_min),
        easy_pace_max=_format_pace(paces.easy_max),
        marathon_pace=_format_pace(paces.marathon),
        threshold_pace=_format_pace(paces.threshold),
        interval_pace=_format_pace(paces.interval),
        repetition_pace=_format_pace(paces.repetition),
        resting_hr=rhr,
        hr_z1_max=zones.z1_max,
        hr_z2_max=zones.z2_max,
        hr_z3_max=zones.z3_max,
        hr_z4_max=zones.z4_max,
        protein_target_g=profile.protein_target_g,
        water_target_l=profile.water_target_l,
    )


def _build_daily_message(
    health: HealthData,
    verdict: RuleVerdict,
    context: WeeklyContext,
    target_date: date,
) -> str:
    """建構每日訓練請求的 user message。"""
    weekday_name = _DAY_NAMES.get(target_date.weekday(), "")
    parts = [
        f"## 今日日期：{target_date.isoformat()}（{weekday_name}）\n",
        "## 規則引擎判定",
        f"- 建議等級：**{verdict.readiness_level}**",
        f"- 建議強度：{verdict.recommended_intensity}",
        f"- 最高心率區間：Z{verdict.max_hr_zone}",
    ]

    if verdict.warnings:
        parts.append(f"- 警告：{'; '.join(verdict.warnings)}")
    if verdict.flags:
        parts.append(f"- 旗標：{', '.join(verdict.flags)}")

    parts.append("\n## 今日 Garmin 數據")
    parts.append(f"- Training Readiness: {health.training_readiness}")
    parts.append(f"- Body Battery: {health.body_battery}")
    parts.append(f"- HRV 昨晚: {health.hrv_last_night} ms")
    parts.append(f"- HRV 七日均值: {health.hrv_weekly_avg} ms")
    parts.append(f"- HRV 狀態: {health.hrv_status}")
    parts.append(f"- 睡眠: {health.sleep_hours}h (分數: {health.sleep_score})")
    parts.append(f"- 壓力: {health.stress}/100")
    parts.append(f"- 恢復時間: {health.recovery_time}h")
    parts.append(f"- 安靜心率: {health.resting_heart_rate} bpm")

    parts.append("\n## 近期訓練摘要")
    parts.append(f"- 過去 {context.days} 天跑步: {context.run_count} 次, 共 {context.total_distance_km} km")
    parts.append(f"- 重訓: {context.strength_count} 次")
    parts.append(f"- 平均睡眠分數: {context.avg_sleep_score}")
    parts.append(f"- 平均 HRV: {context.avg_hrv}")
    if context.chronic_load is not None:
        parts.append(f"- 慢性訓練負荷: {context.chronic_load}")

    if context.activity_records:
        parts.append("\n## 最近活動")
        for act in context.activity_records[:5]:
            line = f"- {act.start_time}: {act.activity_type}"
            if act.distance_km:
                line += f" {act.distance_km}km"
            if act.pace:
                line += f" @ {act.pace}/km"
            if act.avg_hr:
                line += f" HR{act.avg_hr}"
            parts.append(line)

    parts.append("\n請根據以上資訊，給出今日的訓練處方（純 JSON 格式）。")
    return "\n".join(parts)


def _format_weekly_data(context: WeeklyContext) -> str:
    """格式化每週數據為文字。"""
    parts = [
        f"- 涵蓋天數: {context.days}",
        f"- 跑步: {context.run_count} 次, 共 {context.total_distance_km} km",
        f"- 重訓: {context.strength_count} 次",
        f"- 總訓練負荷: {context.total_training_load}",
        f"- 平均睡眠分數: {context.avg_sleep_score}",
        f"- 平均 HRV: {context.avg_hrv}",
        f"- 平均安靜心率: {context.avg_resting_hr}",
        f"- 慢性負荷: {context.chronic_load}",
    ]

    if context.activity_records:
        parts.append("\n### 活動明細")
        for act in context.activity_records:
            line = f"- {act.start_time}: {act.activity_type}"
            if act.distance_km:
                line += f" {act.distance_km}km"
            if act.pace:
                line += f" @ {act.pace}/km"
            if act.avg_hr:
                line += f" HR{act.avg_hr}"
            if act.training_effect_aerobic:
                line += f" TE-A{act.training_effect_aerobic}"
            parts.append(line)

    return "\n".join(parts)


# ── 回應解析 ─────────────────────────────────────────────────────────────────


def _parse_prescription(raw: str, target_date: date) -> Optional[TrainingPrescription]:
    """解析 Claude JSON 回應為 TrainingPrescription。

    Args:
        raw: Claude 的文字回應（應為純 JSON）。
        target_date: 目標日期。

    Returns:
        解析成功回傳 TrainingPrescription，失敗回傳 None。
    """
    text = raw.strip()
    # 移除可能的 markdown code block
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("JSON 解析失敗：%s\n原始回應: %s", exc, raw[:500])
        return None

    try:
        return TrainingPrescription(
            date=target_date.isoformat(),
            session_type=data.get("session_type", ""),
            description=data.get("description", ""),
            target_distance_km=data.get("target_distance_km"),
            target_pace=data.get("target_pace"),
            target_hr_zone=data.get("target_hr_zone"),
            warmup=data.get("warmup", ""),
            main_set=data.get("main_set", ""),
            cooldown=data.get("cooldown", ""),
            strength_notes=data.get("strength_notes"),
            nutrition_notes=data.get("nutrition_notes"),
            warnings=tuple(data.get("warnings", [])),
            confidence=data.get("confidence", "MEDIUM"),
            confidence_reason=data.get("confidence_reason", ""),
        )
    except Exception as exc:
        logger.error("TrainingPrescription 建構失敗：%s", exc)
        return None


# ── Fallback ─────────────────────────────────────────────────────────────────


def _build_fallback_prescription(
    verdict: RuleVerdict,
    target_date: date,
) -> TrainingPrescription:
    """當 Claude API 不可用時，根據規則引擎判定生成基本處方。"""
    level = verdict.readiness_level

    if level == "REST":
        return TrainingPrescription(
            date=target_date.isoformat(),
            session_type="Rest",
            description="規則引擎建議完全休息。今天以恢復為主，可做輕度伸展。",
            warmup="無",
            main_set="完全休息或 10-15 分鐘輕度伸展",
            cooldown="無",
            nutrition_notes="維持蛋白質攝取，多喝水",
            warnings=verdict.warnings,
            confidence="HIGH",
            confidence_reason="規則引擎判定 REST，無需 AI 介入",
        )

    if level == "EASY":
        return TrainingPrescription(
            date=target_date.isoformat(),
            session_type="Recovery",
            description="今日適合輕鬆恢復跑或散步。",
            target_distance_km=5.0,
            target_pace="6:30-7:00",
            target_hr_zone=2,
            warmup="5 分鐘快走",
            main_set="30-40 分鐘輕鬆跑，維持 Z2 以下",
            cooldown="5 分鐘慢走 + 伸展",
            nutrition_notes="跑後補充蛋白質和碳水",
            warnings=verdict.warnings,
            confidence="MEDIUM",
            confidence_reason="Fallback 處方：根據規則引擎 EASY 等級",
        )

    if level == "MODERATE":
        return TrainingPrescription(
            date=target_date.isoformat(),
            session_type="LSD",
            description="今日可進行中等強度訓練。",
            target_distance_km=8.0,
            target_pace="6:15-6:45",
            target_hr_zone=3,
            warmup="10 分鐘慢跑",
            main_set="40-50 分鐘穩定配速跑，不超過 Z3",
            cooldown="10 分鐘慢跑 + 伸展",
            nutrition_notes="訓練前補充碳水化合物",
            warnings=verdict.warnings,
            confidence="MEDIUM",
            confidence_reason="Fallback 處方：根據規則引擎 MODERATE 等級",
        )

    # HIGH
    return TrainingPrescription(
        date=target_date.isoformat(),
        session_type="Tempo",
        description="狀態良好，可進行高強度訓練。",
        target_distance_km=10.0,
        target_pace="5:15-5:30",
        target_hr_zone=4,
        warmup="15 分鐘慢跑 + 動態伸展",
        main_set="20-25 分鐘節奏跑 @ T 配速",
        cooldown="10 分鐘慢跑 + 伸展",
        nutrition_notes="高強度日增加碳水攝取，減少赤字",
        warnings=verdict.warnings,
        confidence="MEDIUM",
        confidence_reason="Fallback 處方：根據規則引擎 HIGH 等級",
    )


def _build_fallback_weekly_review(context: WeeklyContext) -> str:
    """API 失敗時的簡易每週摘要。"""
    return (
        f"📊 本週訓練摘要\n"
        f"- 跑步: {context.run_count} 次 / {context.total_distance_km} km\n"
        f"- 重訓: {context.strength_count} 次\n"
        f"- 平均睡眠分數: {context.avg_sleep_score}\n"
        f"- 平均 HRV: {context.avg_hrv}\n"
        f"- 訓練負荷: {context.total_training_load}\n"
        f"\n⚠️ AI 回顧暫時無法生成，以上為自動摘要。"
    )


# ── 工具函式 ─────────────────────────────────────────────────────────────────


def _format_pace(pace_float: float) -> str:
    """將配速浮點數轉為字串（6.5 → '6:30'）。"""
    minutes = int(pace_float)
    seconds = int((pace_float - minutes) * 60)
    return f"{minutes}:{seconds:02d}"
