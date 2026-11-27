"""utils/run_ledger.py — 生產排程「有沒有跑成功」的極簡帳本。

各生產腳本（health_tracker、investment_tracker、daily_adjust、
training_advisor、weekly_review）main() 收尾時呼叫 :func:`append_run`，
把一行 JSON append 到 ``logs/run_ledger.jsonl``::

    {"script": "...", "date": "...", "ok": true/false, "wrote_notion": true/false,
     "logged_at": "..."}

``append_run`` 內部把所有例外吞掉只記 ``logger.warning`` — 這支帳本掛在
07:00/21:00 的生產管線收尾處，帳本本身故障不能讓正常執行的腳本失敗。

``scripts/watchdog.py``（T09 死人開關）讀 :func:`last_success_time` 判斷
Tier-1 管線是否 48 小時無成功紀錄。
"""

import json
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_RUN_LEDGER_PATH = Path(__file__).resolve().parent.parent / "logs" / "run_ledger.jsonl"


def append_run(
    script: str,
    ok: bool,
    wrote_notion: bool,
    run_date: Optional[date] = None,
) -> None:
    """把一筆執行結果 append 到 ``run_ledger.jsonl``。永不拋出例外。

    Args:
        script: 腳本檔名（例如 ``'health_tracker.py'``）。
        ok: 這次執行是否成功（無未捕捉的致命錯誤）。
        wrote_notion: 這次執行是否實際寫入 Notion（純推送/建議類腳本一律為 False）。
        run_date: 業務日期（例如資料所屬日期），預設今天。真正判斷「多久沒成功」
            用的是 ``logged_at``（實際寫入時間），不是這個欄位。
    """
    try:
        _RUN_LEDGER_PATH.parent.mkdir(exist_ok=True)
        payload = {
            "script": script,
            "date": (run_date or date.today()).isoformat(),
            "ok": bool(ok),
            "wrote_notion": bool(wrote_notion),
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(_RUN_LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("run_ledger 寫入失敗（忽略，不影響主流程）：%s", exc)


def read_recent_runs(script: Optional[str] = None, limit: int = 1000) -> List[dict]:
    """讀取 ``run_ledger.jsonl`` 中的紀錄。

    Args:
        script: 只回傳這個腳本名稱的紀錄；``None`` 回傳全部腳本。
        limit: 僅保留檔案末尾的 N 行，再解析與過濾；非正值回傳空清單。
            串流掃描檔案，記憶體只保留末尾 N 行，不隨帳本總行數增長。

    Returns:
        dict 清單，依檔案內順序（舊到新）。空白、無法解析及非 JSON 物件的行
        一律略過；檔案不存在或讀取失敗時回傳空清單。
    """
    if limit <= 0 or not _RUN_LEDGER_PATH.exists():
        return []

    try:
        with _RUN_LEDGER_PATH.open("r", encoding="utf-8") as ledger_file:
            lines = deque(ledger_file, maxlen=limit)
    except Exception as exc:
        logger.warning("run_ledger 讀取失敗（忽略）：%s", exc)
        return []

    records: List[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if script is None or record.get("script") == script:
            records.append(record)
    return records


def last_success_time(script: str) -> Optional[datetime]:
    """回傳指定腳本最後一次成功（JSON ``ok: true``）執行的時間。

    Args:
        script: 腳本檔名。

    Returns:
        有效成功紀錄中最大的 ``logged_at`` 時間；忽略非布林成功旗標及無效
        ISO 格式時間字串，查無有效成功紀錄時回傳 ``None``。
    """
    timestamps: List[datetime] = []
    for record in read_recent_runs(script):
        if record.get("ok") is not True:
            continue
        logged_at = record.get("logged_at")
        if not isinstance(logged_at, str):
            continue
        try:
            timestamps.append(datetime.fromisoformat(logged_at))
        except ValueError:
            continue
    return max(timestamps) if timestamps else None
