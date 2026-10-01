"""scripts/telegram_bot.py — Telegram 訓練助理推送。

提供兩種模式：
1. **推送模式**（push-only）：daily_adjust / weekly_review / watchdog 等腳本
   呼叫 send_text() / send_alert() 發送訊息
2. **互動模式**（polling）：獨立執行，支援 /today、/week、/rpe、/niggle、/fuel 指令

推送模式使用方式::

    from scripts.telegram_bot import send_alert, send_text

    send_text("訊息內容")
    send_alert("script.py", "錯誤描述")

互動模式::

    python scripts/telegram_bot.py
"""

import functools
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable, TYPE_CHECKING, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import get_logger

if TYPE_CHECKING:
    # python-telegram-bot 是選用依賴（互動模式才需要），型別提示用字串 forward-ref
    # 避免模組載入時就要求安裝它（推送模式 / daily_adjust 等只 import 這個模組的
    # 推送函式，不應因為缺這個套件而 import 失敗）。
    from telegram import Update
    from telegram.ext import ContextTypes

logger = get_logger(__name__)

# ── /rpe、/niggle、/fuel 寫入 Notion 失敗時的本機暫存檔 ─────────────────────
_RPE_PENDING_LOG = Path(__file__).resolve().parent.parent / "logs" / "rpe_pending.jsonl"

# ── 訊息長度防護（T17）───────────────────────────────────────────────────────
# Telegram sendMessage 的 text 欄位上限為 4096 字元。過去 _send_message 完全
# 沒處理這件事——超長時 API 直接回 400，訊息整則消失。
#
# 採用區段截斷以維持單一整合訊息。daily_adjust 的四段式
# 日報本來就依重要性遞減排列（①今日訓練 → ②補給 → ③睡眠 → ④系統心跳），
# 尾端截斷天然對應「先犧牲最不重要的資訊」，不需要額外設計哪段該留。
_TELEGRAM_MAX_LEN = 4096
_TRUNCATION_SUFFIX = "\n\n…（訊息過長，已截斷；完整內容見 logs/advice_log.jsonl）"
# **bold** → <b>bold</b> 等 HTML 轉換會讓字元數變多，保守預留展開空間，避免
# 轉換後才超過上限。
_HTML_EXPANSION_SAFETY_MARGIN = 200


# ── 推送函式（供 daily_adjust / weekly_review / watchdog 等腳本呼叫）─────────


def send_text(
    text: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """推送任意文字訊息。

    Args:
        text: 訊息文字。
        bot_token: Telegram Bot Token。
        chat_id: Telegram Chat ID。

    Returns:
        發送成功回傳 True。
    """
    token, cid = _resolve_credentials(bot_token, chat_id)
    if not token or not cid:
        return False
    return _send_message(token, cid, text)


def send_alert(
    script_name: str,
    error_message: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """推送腳本失敗警告到 Telegram。

    用於 health_tracker / training_advisor 等自動化腳本失敗時的通知。
    若 Telegram 未設定，靜默失敗（不影響主流程）。

    Args:
        script_name: 失敗的腳本名稱。
        error_message: 錯誤訊息（簡短描述）。
        bot_token: Telegram Bot Token。
        chat_id: Telegram Chat ID。

    Returns:
        通知發送成功回傳 True；Telegram 未設定回傳 False（不視為錯誤）。
    """
    text = (
        f"⚠️ **LifeOS 腳本失敗**\n\n"
        f"📜 腳本：`{script_name}`\n"
        f"❌ 錯誤：{error_message}\n\n"
        f"請手動檢查或重新執行。"
    )
    return send_text(text, bot_token=bot_token, chat_id=chat_id)


def safe_send_alert(script_name: str, error_message: str) -> None:
    """無例外版本的 send_alert，可在任何地方呼叫。

    Telegram 未設定或推送失敗時都不會拋例外，避免干擾主流程。
    """
    try:
        send_alert(script_name, error_message)
    except Exception as exc:
        logger.warning("Telegram 警告推送失敗（忽略）：%s", exc)


# ── Telegram API ─────────────────────────────────────────────────────────────


def _truncate_for_telegram(text: str, limit: int = _TELEGRAM_MAX_LEN) -> str:
    """把過長訊息截斷至 Telegram 4096 字元上限以內（T17）。

    在轉成 HTML 前的原始 Markdown 文字上截斷（而非截斷 HTML 之後的字串）：
    ``_markdown_to_html`` 用非貪婪 regex 只轉換「完整配對」的 ``**粗體**`` /
    `` `code` ``，被截斷、只剩一半的標記不會被轉成 ``<b>``/``<code>`` 標籤，
    只會殘留字面上的 ``**`` 或反引號，不會產生 Telegram 拒絕的未封閉 HTML
    標籤。截斷點會退回最近的換行處，避免把某一行硬生生切一半。

    Args:
        text: 原始（未轉 HTML）訊息文字。
        limit: Telegram 單則訊息字元上限（預設 4096）。

    Returns:
        未超限時原樣回傳；超限時在安全邊界截斷並附加提示字樣。
    """
    if len(text) <= limit:
        return text

    budget = max(0, limit - len(_TRUNCATION_SUFFIX) - _HTML_EXPANSION_SAFETY_MARGIN)
    truncated = text[:budget]

    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        truncated = truncated[:last_newline]

    return truncated + _TRUNCATION_SUFFIX


def _send_message(token: str, chat_id: str, text: str) -> bool:
    """透過 Telegram Bot API 發送訊息。

    Args:
        token: Bot Token。
        chat_id: Chat ID。
        text: 訊息文字。

    Returns:
        發送成功回傳 True。
    """
    import requests

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # 長度防護（T17）：先在原始 Markdown 文字截斷，再轉 HTML（見
    # _truncate_for_telegram docstring：這個順序才不會切斷標籤）。
    text = _truncate_for_telegram(text)

    # 用 HTML 模式（比 Markdown 穩定，特殊字元不會誤判）
    # 把常見 Markdown 語法轉成 HTML
    html_text = _markdown_to_html(text)

    # 保底二次防護：_HTML_EXPANSION_SAFETY_MARGIN 理論上已預留足夠空間，
    # 但標籤展開幅度不是嚴格上界，這裡再做一次硬上限，避免邊界案例被
    # Telegram 以 400 拒絕整則訊息。
    if len(html_text) > _TELEGRAM_MAX_LEN:
        html_text = html_text[:_TELEGRAM_MAX_LEN]

    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            logger.info("Telegram 訊息發送成功")
            return True
        else:
            logger.error(
                "Telegram 發送失敗：%d %s", resp.status_code, resp.text[:200]
            )
            # HTML 解析失敗時，改用純文字重試
            if resp.status_code == 400 and "parse" in resp.text.lower():
                logger.info("HTML 解析失敗，改用純文字重試")
                payload.pop("parse_mode")
                payload["text"] = text  # 純文字版本（已截斷）
                retry = requests.post(url, json=payload, timeout=30)
                if retry.status_code == 200:
                    logger.info("純文字重試成功")
                    return True
            return False
    except Exception as exc:
        logger.error("Telegram 發送失敗：%s", exc)
        return False


def _markdown_to_html(text: str) -> str:
    """把 Markdown 語法轉成 Telegram HTML（更穩定）。

    支援轉換：
    - **bold** → <b>bold</b>
    - *italic* → <i>italic</i>
    - `code` → <code>code</code>
    - HTML 危險字元先轉義（&、<、>）
    """
    import re

    # 先轉義 HTML 特殊字元（避免 < > 被當成 tag）
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # **bold** → <b>bold</b>（非貪婪）
    out = re.sub(r"\*\*([^\*]+?)\*\*", r"<b>\1</b>", out)

    # `code` → <code>code</code>
    out = re.sub(r"`([^`]+?)`", r"<code>\1</code>", out)

    return out


def _resolve_credentials(
    bot_token: Optional[str],
    chat_id: Optional[str],
) -> tuple:
    """取得 Telegram 認證資訊。"""
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    cid = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN 未設定")
    if not cid:
        logger.error("TELEGRAM_CHAT_ID 未設定")
    return token, cid


# ── /rpe、/niggle、/fuel 記錄（模組層函式，供測試直接呼叫）───────────────────
#
# 抽成模組層函式（而非 _run_polling() 內的巢狀 closure）是因為巢狀 async 函式
# 無法在 pytest 中直接 import 測試。_run_polling() 內的 CommandHandler 註冊
# 指向這裡定義的函式。


def _append_pending_log(record: dict) -> None:
    """把無法立即寫入 Notion 的 RPE/Niggle 記錄暫存到本機 JSONL 檔。

    Args:
        record: 要暫存的記錄（會補上 ``logged_at`` 時間戳）。
    """
    _RPE_PENDING_LOG.parent.mkdir(exist_ok=True)
    payload = dict(record)
    payload.setdefault("logged_at", datetime.now().isoformat(timespec="seconds"))
    with open(_RPE_PENDING_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _find_today_activity_page(client: object, database_id: str) -> Optional[dict]:
    """查詢 Activity DB 中「今天」的活動頁面。

    Args:
        client: Notion Client 物件。
        database_id: Activity Database ID。

    Returns:
        今天的活動頁面 dict；查無資料或查詢失敗時回傳 None
        （health_tracker_web.py 未排程時，「今天沒有活動」是常態，不是例外）。
    """
    from notion_client_helper import query_pages

    today_iso = date.today().isoformat()
    filter_dict = {"property": "Date", "date": {"equals": today_iso}}
    pages = query_pages(client, database_id, filter_dict=filter_dict, page_size=1)
    return pages[0] if pages else None


def _merge_notes(page: dict, label: str, notes: str) -> Optional[str]:
    """把新的備註附加到頁面既有的 Notes 欄位之後（不覆蓋既有內容）。

    Args:
        page: Notion 頁面 dict（含 properties）。
        label: 備註標籤（如 ``'RPE'`` 或 ``'Niggle'``）。
        notes: 使用者輸入的備註文字（可為空字串）。

    Returns:
        合併後的 Notes 文字；``notes`` 為空字串時回傳 None（代表不更新 Notes 欄位）。
    """
    if not notes:
        return None

    from scripts.notion_reader import _get_rich_text

    existing = _get_rich_text(page.get("properties", {}), "Notes") or ""
    new_line = f"[{label}] {notes}"
    return f"{existing}\n{new_line}" if existing else new_line


async def _record_metric(
    update: "Update",
    label: str,
    property_name: str,
    value: int,
    notes: str,
    extra_reply: str = "",
) -> None:
    """``/rpe``、``/niggle`` 共用的寫入邏輯。

    流程：找當天的 Activity DB 頁面 → ``update_page`` 寫入數值 + 備註。
    找不到當天活動、Notion 未設定、或寫入失敗時，一律暫存到
    ``logs/rpe_pending.jsonl`` 並在回覆中誠實反映「尚未寫入 Notion」，
    絕不假裝已成功記錄。

    Args:
        update: Telegram ``Update`` 物件（用來回覆訊息）。
        label: 記錄類型標籤（``'RPE'`` 或 ``'Niggle'``），用於備註前綴與暫存記錄。
        property_name: 要寫入的 Notion number 屬性名稱
            （``'RPE'`` 或 ``'Niggle Score'``）。
        value: 要寫入的數值。
        notes: 使用者輸入的備註文字。
        extra_reply: 附加在回覆訊息最後的文字（例如 niggle ≥3 的警示），
            無論成功或暫存都會附加。
    """
    from notion_client_helper import (
        get_notion_client,
        prop_number,
        prop_rich_text,
        update_page,
    )

    today_iso = date.today().isoformat()

    def _pending() -> None:
        _append_pending_log(
            {"type": label.lower(), "date": today_iso, "value": value, "notes": notes}
        )

    from config import get_activity_api_key

    api_key = get_activity_api_key()  # Activity DB 在獨立 workspace，須用專屬 key
    db_id = os.getenv("ACTIVITY_DB_ID", "")

    if not api_key or not db_id:
        _pending()
        await update.message.reply_text(
            f"📝 NOTION_API_KEY/ACTIVITY_DB_ID 未設定，{label} {value} 已暫存到本機"
            f"（logs/rpe_pending.jsonl），尚未寫入 Notion。" + extra_reply
        )
        return

    try:
        client = get_notion_client(api_key)
        page = _find_today_activity_page(client, db_id)

        if page is None:
            _pending()
            await update.message.reply_text(
                f"📝 今天沒有活動紀錄，{label} {value} 已暫存到本機"
                f"（logs/rpe_pending.jsonl），尚未寫入 Notion。" + extra_reply
            )
            return

        properties: dict = {property_name: prop_number(value)}
        merged_notes = _merge_notes(page, label, notes)
        if merged_notes is not None:
            properties["Notes"] = prop_rich_text(merged_notes)

        result = update_page(client, page_id=page["id"], properties=properties)
        if result is None:
            _pending()
            await update.message.reply_text(
                f"❌ 寫入失敗：Notion 更新發生錯誤，{label} {value} 已暫存到本機"
                f"（logs/rpe_pending.jsonl），請稍後確認。" + extra_reply
            )
            return

        note_suffix = f"（{notes}）" if notes else ""
        await update.message.reply_text(
            f"✅ 已寫入 Notion：{label} {value}{note_suffix}" + extra_reply
        )

    except Exception as exc:
        logger.error("%s 寫入 Notion 時發生未預期錯誤：%s", label, exc)
        _pending()
        await update.message.reply_text(
            f"❌ 寫入失敗：{exc}\n{label} {value} 已暫存到本機"
            f"（logs/rpe_pending.jsonl）。" + extra_reply
        )


async def cmd_rpe(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """記錄訓練 RPE 並寫入 Notion。用法: /rpe 7 小腿有點緊。"""
    args = context.args
    if not args:
        await update.message.reply_text("用法: /rpe <1-10> [備註]\n例如: /rpe 7 小腿有點緊")
        return

    try:
        rpe = int(args[0])
        if not 1 <= rpe <= 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("RPE 必須是 1-10 的整數")
        return

    notes = " ".join(args[1:]) if len(args) > 1 else ""
    await _record_metric(update, "RPE", "RPE", rpe, notes)


async def _record_fuel(
    update: "Update",
    carbs_g: int,
    gi_score: int,
    notes: str,
    extra_reply: str = "",
) -> None:
    """``/fuel`` 的寫入邏輯（複用 T07 的「查當日 Activity 頁 + update_page」路徑）。

    與 ``_record_metric`` 同一套誠實回報約定：找不到當天活動、Notion 未設定、
    或寫入失敗（包含 Activity DB 尚未跑 ``add_activity_fuel_fields.js`` 遷移、
    欄位不存在的情況）時，一律暫存到 ``logs/rpe_pending.jsonl`` 並在回覆中
    誠實反映「尚未寫入 Notion」，絕不假裝已成功記錄。

    寫入三個欄位：``Fuel Carbs (g)``（number）、``GI Score``（number）、
    ``Fuel Plan``（rich_text，僅在有備註時更新）。

    Args:
        update: Telegram ``Update`` 物件（用來回覆訊息）。
        carbs_g: 該次訓練賽中總攝取碳水（g）。
        gi_score: 腸胃耐受分數（1=完全沒事, 5=嚴重不適）。
        notes: 補給內容備註（寫入 ``Fuel Plan``，可為空字串）。
        extra_reply: 附加在回覆訊息最後的文字（例如 GI ≥3 的調整提示），
            無論成功或暫存都會附加。
    """
    from notion_client_helper import (
        get_notion_client,
        prop_number,
        prop_rich_text,
        update_page,
    )

    today_iso = date.today().isoformat()
    display = f"碳水 {carbs_g}g / GI {gi_score}"

    def _pending() -> None:
        _append_pending_log(
            {
                "type": "fuel",
                "date": today_iso,
                "carbs_g": carbs_g,
                "gi_score": gi_score,
                "notes": notes,
            }
        )

    from config import get_activity_api_key

    api_key = get_activity_api_key()  # Activity DB 在獨立 workspace，須用專屬 key
    db_id = os.getenv("ACTIVITY_DB_ID", "")

    if not api_key or not db_id:
        _pending()
        await update.message.reply_text(
            f"📝 NOTION_API_KEY/ACTIVITY_DB_ID 未設定，{display} 已暫存到本機"
            f"（logs/rpe_pending.jsonl），尚未寫入 Notion。" + extra_reply
        )
        return

    try:
        client = get_notion_client(api_key)
        page = _find_today_activity_page(client, db_id)

        if page is None:
            _pending()
            await update.message.reply_text(
                f"📝 今天沒有活動紀錄，{display} 已暫存到本機"
                f"（logs/rpe_pending.jsonl），尚未寫入 Notion。" + extra_reply
            )
            return

        properties: dict = {
            "Fuel Carbs (g)": prop_number(carbs_g),
            "GI Score": prop_number(gi_score),
        }
        if notes:
            properties["Fuel Plan"] = prop_rich_text(notes)

        result = update_page(client, page_id=page["id"], properties=properties)
        if result is None:
            _pending()
            await update.message.reply_text(
                f"❌ 寫入失敗：Notion 更新發生錯誤，{display} 已暫存到本機"
                f"（logs/rpe_pending.jsonl），請稍後確認。" + extra_reply
            )
            return

        note_suffix = f"（{notes}）" if notes else ""
        await update.message.reply_text(
            f"✅ 已寫入 Notion：{display}{note_suffix}" + extra_reply
        )

    except Exception as exc:
        logger.error("Fuel 寫入 Notion 時發生未預期錯誤：%s", exc)
        _pending()
        await update.message.reply_text(
            f"❌ 寫入失敗：{exc}\n{display} 已暫存到本機"
            f"（logs/rpe_pending.jsonl）。" + extra_reply
        )


async def cmd_fuel(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """記錄長跑賽中補給（腸胃訓練）並寫入 Notion。用法: /fuel 45 2 兩包gel。

    GI 腸胃耐受：1=完全沒事, 5=嚴重不適。GI ≥3 時在回覆中提示下次
    維持或降低補給量、考慮更換品項（training_advisor 讀得到同一訊號）。
    """
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "用法: /fuel <碳水g> <GI 1-5> [補給內容]\n"
            "例如: /fuel 45 2 兩包gel+500ml電解質\n"
            "GI 腸胃耐受：1=完全沒事, 5=嚴重不適"
        )
        return

    try:
        carbs = int(args[0])
        if not 0 <= carbs <= 500:
            raise ValueError
    except ValueError:
        await update.message.reply_text("碳水必須是 0-500 的整數（克）")
        return

    try:
        gi = int(args[1])
        if not 1 <= gi <= 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("GI 分數必須是 1-5 的整數（1=完全沒事, 5=嚴重不適）")
        return

    notes = " ".join(args[2:]) if len(args) > 2 else ""
    extra_reply = ""
    if gi >= 3:
        extra_reply = "\n⚠️ GI ≥3（腸胃不適），下次長跑維持或降低補給量、考慮更換品項。"

    await _record_fuel(update, carbs, gi, notes, extra_reply=extra_reply)


async def cmd_niggle(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """記錄痠痛/傷害前兆分數並寫入 Notion。用法: /niggle 4 左膝。

    0=無異樣，5=疼痛影響跑姿。分數 ≥3 時會在回覆中提示考慮降低強度或休跑，
    好讓 daily_adjust.py 隔天讀得到這個訊號。
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "用法: /niggle <0-5> [部位備註]\n例如: /niggle 4 左膝\n"
            "0=無異樣, 5=疼痛影響跑姿"
        )
        return

    try:
        niggle = int(args[0])
        if not 0 <= niggle <= 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Niggle 分數必須是 0-5 的整數")
        return

    notes = " ".join(args[1:]) if len(args) > 1 else ""
    extra_reply = ""
    if niggle >= 3:
        extra_reply = "\n⚠️ 痠痛分數 ≥3，建議考慮降低強度或休跑，並觀察是否需要就醫。"

    await _record_metric(
        update, "Niggle", "Niggle Score", niggle, notes, extra_reply=extra_reply
    )


# ── 輪詢模式 chat_id 白名單 ───────────────────────────────────────────────────
#
# 輪詢模式對任何找到 bot 的 Telegram 使用者開放（可讀個人健康/營養/身體數據、
# 觸發付費 OpenRouter 呼叫），必須在指令真正執行前擋掉非本人來源。
# 非白名單來源一律「靜默忽略」：不回覆任何內容（避免讓對方確認 bot 存在），
# 只記錄一行 WARNING 含來源 chat_id。


def _is_authorized_chat(update: "Update") -> bool:
    """檢查訊息來源是否為白名單 chat_id（``TELEGRAM_CHAT_ID``）。

    使用 ``update.effective_chat``（而非只看 ``update.message.chat``），
    這樣未來若加上 callback/inline query 處理器也能沿用同一個判斷式。

    Args:
        update: Telegram ``Update`` 物件。

    Returns:
        來源 chat id 與 ``TELEGRAM_CHAT_ID`` 相符時回傳 True；
        ``TELEGRAM_CHAT_ID`` 未設定、來源不明、或 id 無法比較時一律回傳 False
        （fail-closed，寧可誤擋也不誤放）。
    """
    allowed = os.getenv("TELEGRAM_CHAT_ID", "")
    if not allowed:
        return False

    chat = getattr(update, "effective_chat", None)
    if chat is None or getattr(chat, "id", None) is None:
        return False

    try:
        return int(chat.id) == int(allowed)
    except (TypeError, ValueError):
        return False


def _authorized_only(
    handler: Callable[["Update", "ContextTypes.DEFAULT_TYPE"], Awaitable[None]],
) -> Callable[["Update", "ContextTypes.DEFAULT_TYPE"], Awaitable[None]]:
    """裝飾器：只有白名單 chat_id 的訊息才會呼叫實際的指令處理函式。

    非白名單來源不回覆、不執行任何後續邏輯（不查 Notion、不呼叫 AI），
    只記錄一行 WARNING。

    Args:
        handler: 要保護的 async 指令處理函式（``cmd_*``）。

    Returns:
        包一層白名單檢查的 async 函式，簽名與 ``handler`` 相同。
    """

    @functools.wraps(handler)
    async def wrapper(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _is_authorized_chat(update):
            chat = getattr(update, "effective_chat", None)
            chat_id = getattr(chat, "id", None)
            logger.warning(
                "Telegram 收到非白名單來源的指令，已忽略（不回覆）。來源 chat_id=%s",
                chat_id,
            )
            return
        await handler(update, context)

    return wrapper


# ── 互動模式（獨立執行）────────────────────────────────────────────────────


def _run_polling() -> None:
    """啟動 Telegram Bot polling 模式，支援互動指令。"""
    try:
        from telegram import Update
        from telegram.ext import (
            Application,
            CommandHandler,
            ContextTypes,
        )
    except ImportError:
        logger.error(
            "python-telegram-bot 未安裝。互動模式需要: pip install python-telegram-bot"
        )
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN 未設定")
        return

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """歡迎訊息 + 指令清單。"""
        await update.message.reply_text(
            "🤖 LifeOS Bot 已就緒\n\n"
            "可用指令：\n"
            "/today — 今日訓練微調（規則引擎 + AI）\n"
            "/week — 本週訓練計劃（Garmin Coach + AI 微調）\n"
            "/health — 最新 Garmin 健康摘要\n"
            "/nutrition — 今日營養攝取\n"
            "/rpe <1-10> [備註] — 記錄訓練感受\n"
            "/niggle <0-5> [部位備註] — 記錄痠痛/傷害前兆（0=無異樣, 5=疼痛影響跑姿）\n"
            "/fuel <碳水g> <GI 1-5> [補給內容] — 長跑後回報賽中補給（腸胃訓練）\n"
            "/help — 顯示此說明"
        )

    async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """觸發今日訓練微調（v3：規則引擎 + AI；取代已除役的 daily_plan）。"""
        await update.message.reply_text("⏳ 正在生成今日訓練微調（規則引擎 + AI）...")
        try:
            from scripts.daily_adjust import run_daily_adjust

            # dry_run=True：訊息由本 handler 直接回覆給白名單使用者，
            # 避免 run_daily_adjust 內部再 send_text 一次造成重複推送。
            result = run_daily_adjust(dry_run=True)
            await update.message.reply_text(
                result or "❌ 生成失敗（無法取得建議，請查看 log）"
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ 生成失敗：{exc}")

    async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """觸發本週訓練計劃（Garmin Coach + AI 微調）。"""
        await update.message.reply_text("⏳ 正在生成本週訓練計劃（Garmin Coach + AI 微調）...")
        try:
            from scripts.training_advisor import run_weekly_advisor
            result = run_weekly_advisor(dry_run=False, output_telegram=True)
            if not result:
                await update.message.reply_text("❌ 計劃生成失敗")
        except Exception as exc:
            await update.message.reply_text(f"❌ 生成失敗：{exc}")

    async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """顯示最新 Garmin 健康摘要。"""
        await update.message.reply_text("⏳ 讀取健康摘要...")
        try:
            from datetime import date, timedelta
            from notion_client_helper import get_notion_client
            from scripts.notion_reader import fetch_health_history

            client = get_notion_client(os.getenv("NOTION_API_KEY", ""))
            health_db = os.getenv("HEALTH_DB_ID", "")
            records = fetch_health_history(client, health_db, days=3)
            if not records:
                await update.message.reply_text("沒有近期健康資料")
                return

            latest = records[0]
            lines = ["❤️ 最新健康摘要"]
            if latest.steps is not None:
                lines.append(f"步數: {latest.steps:,}")
            if latest.resting_heart_rate is not None:
                lines.append(f"安靜心率: {latest.resting_heart_rate} bpm")
            if latest.sleep_hours is not None:
                lines.append(f"睡眠: {latest.sleep_hours} 小時 (分數 {latest.sleep_score or '?'})")
            if latest.body_battery is not None:
                lines.append(f"Body Battery: {latest.body_battery}")
            if latest.training_readiness is not None:
                lines.append(f"Training Readiness: {latest.training_readiness} ({latest.training_readiness_level or '?'})")
            if latest.hrv_last_night is not None:
                lines.append(f"HRV: {latest.hrv_last_night} ms")
            await update.message.reply_text("\n".join(lines))
        except Exception as exc:
            await update.message.reply_text(f"❌ 讀取失敗：{exc}")

    async def cmd_nutrition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """顯示今日營養攝取。"""
        await update.message.reply_text("⏳ 讀取營養資料...")
        try:
            from notion_client_helper import get_notion_client
            from scripts.notion_reader import fetch_nutrition_history

            api_key = os.getenv("NUTRITION_NOTION_API_KEY") or os.getenv("NOTION_API_KEY", "")
            db_id = os.getenv("NUTRITION_DB_ID", "")
            if not db_id:
                await update.message.reply_text("NUTRITION_DB_ID 未設定")
                return

            client = get_notion_client(api_key)
            records = fetch_nutrition_history(client, db_id, days=1)
            if not records:
                await update.message.reply_text("今天還沒有營養紀錄")
                return

            latest = records[0]
            lines = [f"🥗 營養 ({latest.date})"]
            if latest.calories is not None:
                lines.append(f"熱量: {latest.calories:.0f} kcal")
            if latest.protein_g is not None:
                lines.append(f"蛋白質: {latest.protein_g:.1f} g / 160g 目標")
            if latest.carbs_g is not None:
                lines.append(f"碳水: {latest.carbs_g:.1f} g")
            if latest.fat_g is not None:
                lines.append(f"脂肪: {latest.fat_g:.1f} g")
            if latest.water_ml is not None:
                lines.append(f"水分: {latest.water_ml:.0f} ml / 3000ml 目標")
            if latest.weight_kg is not None:
                lines.append(f"體重: {latest.weight_kg:.1f} kg")
            if latest.body_fat_pct is not None:
                lines.append(f"體脂: {latest.body_fat_pct:.1f}%")
            await update.message.reply_text("\n".join(lines))
        except Exception as exc:
            await update.message.reply_text(f"❌ 讀取失敗：{exc}")

    # cmd_rpe / cmd_niggle 定義在模組層（見上方），而非這裡的巢狀 closure，
    # 這樣才能在 pytest 中直接 import 測試。

    # 所有指令處理器都套上白名單檢查：非本人 chat_id 一律靜默忽略
    # （見 _authorized_only / _is_authorized_chat）。
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _authorized_only(cmd_start)))
    app.add_handler(CommandHandler("help", _authorized_only(cmd_start)))
    app.add_handler(CommandHandler("today", _authorized_only(cmd_today)))
    app.add_handler(CommandHandler("week", _authorized_only(cmd_week)))
    app.add_handler(CommandHandler("health", _authorized_only(cmd_health)))
    app.add_handler(CommandHandler("nutrition", _authorized_only(cmd_nutrition)))
    app.add_handler(CommandHandler("rpe", _authorized_only(cmd_rpe)))
    app.add_handler(CommandHandler("niggle", _authorized_only(cmd_niggle)))
    app.add_handler(CommandHandler("fuel", _authorized_only(cmd_fuel)))

    logger.info("Telegram Bot 啟動（polling 模式）")
    app.run_polling()


if __name__ == "__main__":
    _run_polling()
