"""scripts/monthly_review.py — 每月 1 日 09:00 OKR Scorecard。

讀取過去 4 週週回顧 + 所有 KR 進展，AI 生成月度 OKR Scorecard。

關鍵功能：
1. KR 生死判斷（連續 4 週無進展 → 強制標記為 At Risk 或建議 Drop）
2. OKR Scorecard（達成率 + 趨勢 + 決定）
3. 下月焦點（Top 3 KR）

使用方式::

    python scripts/monthly_review.py            # 推 Telegram
    python scripts/monthly_review.py --dry-run  # 只印不推
"""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from scripts.weekly_review import _build_week_summary, _fetch_active_krs, KeyResult
from utils.logger import get_logger

logger = get_logger(__name__)


def run_monthly_review(dry_run: bool = False) -> Optional[str]:
    """執行月度 OKR Scorecard。"""
    logger.info("=" * 60)
    logger.info("月度 OKR Scorecard — %s", date.today().isoformat())
    logger.info("=" * 60)

    today = date.today()
    # 過去 4 週
    month_start = today - timedelta(days=28)

    # ── 讀取過去 4 週數據 ─────────────────────────────────────────────
    summary = _build_week_summary(month_start, today)
    krs = _fetch_active_krs()

    # ── AI 分析 ────────────────────────────────────────────────────────
    review = _generate_monthly_scorecard(month_start, today, summary, krs)

    if not review:
        logger.error("月度回顧生成失敗")
        return None

    if dry_run:
        logger.info("乾跑模式 — 內容如下：")
        sys.stdout.buffer.write(("\n" + review + "\n").encode("utf-8"))
    else:
        try:
            from scripts.telegram_bot import send_text
            ok = send_text(review)
            if ok:
                logger.info("✅ 已推送至 Telegram")
            else:
                logger.warning("Telegram 推送失敗")
        except Exception as exc:
            logger.error("推送失敗：%s", exc)

    return review


def _generate_monthly_scorecard(
    month_start: date,
    today: date,
    summary: Dict,
    krs: List[KeyResult],
) -> Optional[str]:
    """呼叫 AI 生成月度 Scorecard。"""
    from scripts.ai_coach import _call_ai_api

    period = f"{month_start.isoformat()} ~ {today.isoformat()}"

    # 組摘要區塊
    summary_lines = [f"## 過去 4 週累積數據（{period}）"]
    if "health" in summary:
        h = summary["health"]
        summary_lines.append(
            f"- **健康**（{h.get('days_count', 0)} 天有資料）："
            f"睡眠均 {h.get('avg_sleep_score', '?')} / "
            f"HRV 均 {h.get('avg_hrv_ms', '?')}ms / "
            f"安靜心率均 {h.get('avg_rhr_bpm', '?')}bpm"
        )
    if "activity" in summary:
        a = summary["activity"]
        summary_lines.append(
            f"- **訓練**：跑步 {a.get('run_count', 0)} 次共 {a.get('total_run_km', 0)}km / "
            f"重訓 {a.get('strength_count', 0)} 次 / "
            f"訓練負荷總計 {a.get('total_load', 0)}"
        )
    if "nutrition" in summary:
        n = summary["nutrition"]
        weight_change = n.get("weight_change_kg")
        weight_trend = ""
        if weight_change is not None:
            sign = "+" if weight_change >= 0 else ""
            weight_trend = f"（4 週變化 {sign}{weight_change}kg）"
        summary_lines.append(
            f"- **營養**：熱量均 {n.get('avg_calories', '?')}kcal / "
            f"蛋白質均 {n.get('avg_protein_g', '?')}g / "
            f"水分均 {n.get('avg_water_ml', '?')}ml / "
            f"體重 {n.get('latest_weight_kg', '?')}kg {weight_trend}"
        )

    summary_section = "\n".join(summary_lines)

    # KR 區塊
    kr_section = "## Active Key Results（含過去進展）"
    if krs:
        kr_lines = []
        for kr in krs:
            progress = f"{kr.progress_pct:.0f}%" if kr.progress_pct is not None else "?%"
            current = kr.current if kr.current is not None else "?"
            target = kr.target if kr.target is not None else "?"
            unit = kr.unit or ""
            last_rev = kr.last_reviewed or "從未"
            adj_log = f"\n  歷次調整：{kr.strategy_notes}" if kr.strategy_notes else ""
            kr_lines.append(
                f"- **{kr.name}**（狀態 {kr.status or '?'}）：{current}/{target} {unit} → {progress}"
                f" | 上次檢視 {last_rev}{adj_log}"
            )
        kr_section += "\n" + "\n".join(kr_lines)
    else:
        kr_section += "\n（無 Active KR — 建議先建立本季 OKR）"

    system_prompt = (
        "你是個人成長教練 + OKR 顧問。每月 1 日，根據過去 4 週數據和 KR 進展，"
        "提供月度 OKR Scorecard。\n\n"
        "**核心原則：**\n"
        "1. 每個結論引用具體數字\n"
        "2. KR 生死判斷規則：\n"
        "   - 連續 4 週進度 < 10% → **強制建議 Drop 或大幅調整**\n"
        "   - 達成率 > 80% → **建議拉高目標**\n"
        "   - 連續 2 週無進展 → **檢視策略**\n"
        "3. 區分「KR 設錯了」vs「執行力不夠」（看數據判斷）\n\n"
        "**輸出格式（繁體中文 + Markdown，用 ** 雙星號 標粗體）：**\n\n"
        "**📈 月度 OKR Scorecard（{period}）**\n"
        "**━━━━━━━━━━━━━━**\n\n"
        "**🎯 OKR 總體達成率**：N/A 或計算所有 KR 平均 Progress %\n\n"
        "**🔍 KR 逐條判斷**（每個 KR 一段）：\n"
        "格式：「KR 名稱 | 達成 X% | 趨勢 ↑→↓ | 決定：維持/調整/Drop | 原因」\n\n"
        "**💀 建議 Drop / 大幅調整的 KR**：根據生死判斷規則列出，並給替代方案\n\n"
        "**📊 4 週身體與營養趨勢**：體重變化、訓練量趨勢、HRV 趨勢的整體判斷\n\n"
        "**🚀 下月 Top 3 焦點 KR**：集中火力的 3 個目標\n\n"
        "**💡 系統洞察**：1-2 個跨領域 insight（例如「跑量增加但體重沒降，可能熱量赤字不足」）\n\n"
        "全部不超過 800 字。"
    )

    user_message = f"{summary_section}\n\n{kr_section}"

    response = _call_ai_api(
        system_prompt=system_prompt.replace("{period}", period),
        user_message=user_message,
        max_tokens=2500,
    )

    return response


def main() -> None:
    parser = argparse.ArgumentParser(description="月度 OKR Scorecard")
    parser.add_argument("--dry-run", action="store_true", help="只印出，不推送")
    parser.add_argument("--force", action="store_true", help="強制執行（即使不是月初）")
    args = parser.parse_args()

    # 預設只在每月 1-7 號執行（給 Windows 排程每週跑用）
    if not args.force and not args.dry_run:
        if date.today().day > 7:
            logger.info("今天是 %d 號，非月初範圍，跳過。用 --force 強制執行。", date.today().day)
            sys.exit(0)

    result = run_monthly_review(dry_run=args.dry_run)
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
