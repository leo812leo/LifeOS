"""scripts/backup_notion.py — 全 DB 備份腳本（帶日期 JSON 傾印，T14）。

Notion 是全系統唯一的資料庫，存著兩年多的健康/體重/訓練時間序列資料，目前
零備份。本腳本刻意保持笨拙簡單：

1. 掃描 ``.env`` 中所有非空的 ``*_DB_ID``（不假設固定清單，新增 DB 會自動納入）。
2. 每個 DB 用 :func:`notion_client_helper.query_all_pages` 全量拉取 raw JSON，
   寫到 ``backups/YYYY-MM-DD/<db_name>.json``。
3. 若本機有雲端同步資料夾（OneDrive/Google Drive/Dropbox），複製一份過去做
   異地備援；沒有的話只留本機備份並在摘要註明。
4. 只保留最新 8 份日期資料夾（本機與雲端各自輪替），更舊的自動刪除。
5. 結束推一行 Telegram 摘要（成功/失敗 DB 數），並（若 ``utils/run_ledger.py``
   存在）附加一筆執行紀錄。

多 workspace 架構：Activity / Nutrition / OKR（Areas/Objectives/KR）三組 DB
各自可能位於不同 Notion workspace，需要各自的 Integration Token，沿用
``weekly_review.py`` / ``training_advisor.py`` 既有的 fallback 鏈
（見 :func:`_resolve_api_key`），不可用單一 ``NOTION_API_KEY`` 遍歷。

使用方式::

    python scripts/backup_notion.py             # 實際備份 + 推 Telegram
    python scripts/backup_notion.py --dry-run   # 只列出會備份哪些 DB

建議排程：每週日 22:00（不在本腳本內註冊 Windows Scheduled Task —
依專案慣例，排程註冊是另外的操作步驟）。
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from notion_client_helper import get_notion_client, query_all_pages  # noqa: E402
from utils.logger import get_logger  # noqa: E402

try:
    from utils.run_ledger import append_run
except ImportError:  # pragma: no cover — T09 尚未落地時的軟依賴，直接跳過記帳
    append_run = None  # type: ignore[assignment]

logger = get_logger(__name__)

_BACKUP_ROOT = Path(__file__).resolve().parent.parent / "backups"
_KEEP_BACKUPS = 8
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DB_ID_ENV_RE = re.compile(r"^([A-Z0-9_]+)_DB_ID$")

# 沒有獨立 Integration Token、共用 NOTION_API_KEY 的 OKR 三個 DB。
_OKR_DB_ENV_VARS = ("AREAS_DB_ID", "OBJECTIVES_DB_ID", "KR_DB_ID")


# ── 多 workspace API key 解析 ─────────────────────────────────────────────────


def _resolve_api_key(env_var_name: str, notion_api_key: str) -> str:
    """依 DB 對應的環境變數名稱，決定要用哪把 Notion API Key。

    沿用 ``weekly_review.py:104-107``、``training_advisor.py:452-453`` 既有的
    fallback 鏈：ACTIVITY_DB_ID → ``ACTIVITY_NOTION_API_KEY``；
    NUTRITION_DB_ID → ``NUTRITION_NOTION_API_KEY``；
    AREAS/OBJECTIVES/KR_DB_ID → ``OKR_NOTION_API_KEY`` →
    ``NUTRITION_NOTION_API_KEY`` → ``NOTION_API_KEY``；其餘一律用
    ``NOTION_API_KEY``。

    Args:
        env_var_name: DB ID 的環境變數名稱，例如 ``'ACTIVITY_DB_ID'``。
        notion_api_key: 預設 workspace 的 ``NOTION_API_KEY``（當作最終 fallback）。

    Returns:
        該 DB 應使用的 Notion API Key（可能是空字串，代表完全沒設定）。
    """
    if env_var_name == "ACTIVITY_DB_ID":
        return os.getenv("ACTIVITY_NOTION_API_KEY", notion_api_key)
    if env_var_name == "NUTRITION_DB_ID":
        return os.getenv("NUTRITION_NOTION_API_KEY", notion_api_key)
    if env_var_name in _OKR_DB_ENV_VARS:
        return (
            os.getenv("OKR_NOTION_API_KEY")
            or os.getenv("NUTRITION_NOTION_API_KEY")
            or notion_api_key
        )
    return notion_api_key


# ── DB 探索 ───────────────────────────────────────────────────────────────────


def _discover_databases(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """掃描環境變數，找出所有非空的 ``*_DB_ID``。

    不假設固定的 DB 清單 — 未來新增的 ``*_DB_ID`` 會自動被納入備份範圍，
    未設定（空字串或不存在）的 DB 則自動略過（例如尚未申請的
    ``TRAINING_PLAN_DB_ID``）。

    Args:
        environ: 供測試注入的環境變數 dict；``None`` 時使用 ``os.environ``。

    Returns:
        ``{env_var_name: db_id}`` dict，例如 ``{"HEALTH_DB_ID": "abc123"}``。
    """
    env = environ if environ is not None else os.environ
    return {
        key: value
        for key, value in env.items()
        if _DB_ID_ENV_RE.match(key) and value
    }


def _db_display_name(env_var_name: str) -> str:
    """把 env var 名稱轉成備份檔名友善的顯示名稱。

    例如 ``'HEALTH_DB_ID'`` -> ``'health'``。
    """
    match = _DB_ID_ENV_RE.match(env_var_name)
    base = match.group(1) if match else env_var_name
    return base.lower()


# ── 單一 DB 備份 ───────────────────────────────────────────────────────────────


def _backup_single_db(
    env_var_name: str,
    db_id: str,
    notion_api_key: str,
    dest_dir: Path,
) -> bool:
    """備份單一 Database 的全部頁面到 ``dest_dir/<name>.json``。

    刻意不拋出例外 — 單一 DB 失敗（API key 錯誤、workspace 未分享等）
    不應中斷其他 DB 的備份。

    Args:
        env_var_name: DB ID 環境變數名稱。
        db_id: Notion Database ID。
        notion_api_key: 預設 workspace API Key（供 ``_resolve_api_key`` fallback）。
        dest_dir: 本次備份的目的資料夾（``backups/YYYY-MM-DD/``）。

    Returns:
        成功回傳 True，任何失敗回傳 False。
    """
    name = _db_display_name(env_var_name)
    api_key = _resolve_api_key(env_var_name, notion_api_key)
    if not api_key:
        logger.error("%s：找不到可用的 Notion API Key，略過備份", name)
        return False

    try:
        client = get_notion_client(api_key)
        pages = query_all_pages(client, db_id, raise_on_error=True)
    except Exception as exc:
        logger.error("%s 備份查詢失敗：%s", name, exc)
        return False

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{name}.json"
        dest_path.write_text(
            json.dumps(pages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error("%s 寫入備份檔失敗：%s", name, exc)
        return False

    logger.info("%s：%d 筆記錄已備份", name, len(pages))
    return True


# ── 雲端同步資料夾偵測 + 複製 ──────────────────────────────────────────────────


def _find_cloud_backup_root() -> Optional[Path]:
    """偵測本機是否有雲端同步資料夾（OneDrive/Google Drive/Dropbox）。

    優先讀取 Windows 內建的 ``OneDrive`` / ``OneDriveConsumer`` /
    ``OneDriveCommercial`` 環境變數（比猜路徑可靠），找不到再退回猜測使用者
    家目錄底下常見的資料夾名稱。

    Returns:
        找到的雲端資料夾內、要用來放備份的 ``LifeOS-Backups`` 子目錄路徑；
        本機沒有任何雲端同步資料夾時回傳 ``None``。
    """
    candidates: List[Path] = []

    for env_var in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        value = os.getenv(env_var)
        if value:
            candidates.append(Path(value))

    home = Path.home()
    candidates.extend(
        [
            home / "OneDrive",
            home / "Google Drive",
            home / "GoogleDrive",
            home / "Dropbox",
        ]
    )

    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate / "LifeOS-Backups"
        except OSError:
            continue

    return None


def _copy_to_cloud(dest_dir: Path, today_str: str) -> str:
    """把今日備份複製一份到雲端同步資料夾（如果找到的話）。

    Args:
        dest_dir: 本機今日備份資料夾（``backups/YYYY-MM-DD/``）。
        today_str: 今日日期字串（``YYYY-MM-DD``），用來當作雲端目的地子目錄名。

    Returns:
        給 Telegram 摘要用的一行說明文字。
    """
    cloud_root = _find_cloud_backup_root()
    if cloud_root is None:
        return "無雲端資料夾，僅本機備份"

    try:
        cloud_root.mkdir(parents=True, exist_ok=True)
        cloud_dest = cloud_root / today_str
        if cloud_dest.exists():
            shutil.rmtree(cloud_dest)
        shutil.copytree(dest_dir, cloud_dest)
        _prune_old_backups(cloud_root)
    except OSError as exc:
        logger.warning("複製到雲端資料夾失敗（忽略，本機備份仍保留）：%s", exc)
        return "雲端同步失敗，僅本機備份"

    return f"已同步至雲端資料夾：{cloud_root}"


# ── 輪替：只留最新 N 份 ────────────────────────────────────────────────────────


def _prune_old_backups(root: Path, keep: int = _KEEP_BACKUPS) -> None:
    """只保留 ``root`` 底下最新 ``keep`` 份日期資料夾，其餘刪除。

    只處理名稱符合 ``YYYY-MM-DD`` 格式的子目錄，避免誤刪同一資料夾下其他
    非備份用途的內容。

    Args:
        root: 備份根目錄（本機 ``backups/`` 或雲端 ``LifeOS-Backups/``）。
        keep: 保留的最新份數（預設 8）。
    """
    if not root.is_dir():
        return

    date_dirs = sorted(
        (d for d in root.iterdir() if d.is_dir() and _DATE_DIR_RE.match(d.name)),
        key=lambda d: d.name,
    )

    stale = date_dirs[:-keep] if keep > 0 else date_dirs
    for old_dir in stale:
        try:
            shutil.rmtree(old_dir)
            logger.info("刪除舊備份：%s", old_dir)
        except OSError as exc:
            logger.warning("刪除舊備份失敗（忽略）：%s - %s", old_dir, exc)


# ── Telegram 摘要 + run_ledger ─────────────────────────────────────────────────


def _send_summary(succeeded: List[str], failed: List[str], cloud_note: str) -> None:
    """推送一行 Telegram 備份摘要。Telegram 未設定或推送失敗都不拋出例外。"""
    total = len(succeeded) + len(failed)
    lines = [f"📦 Notion 備份完成：{len(succeeded)}/{total} 個 DB 成功"]
    if failed:
        lines.append(f"❌ 失敗：{', '.join(sorted(failed))}")
    lines.append(cloud_note)
    text = "\n".join(lines)

    try:
        from scripts.telegram_bot import send_text

        if not send_text(text):
            logger.warning("Telegram 推送失敗或未設定")
    except Exception as exc:
        logger.warning("Telegram 推送發生例外（忽略）：%s", exc)


def _log_run(ok: bool) -> None:
    """附加一筆執行紀錄到 run_ledger（軟依賴，``utils/run_ledger.py`` 不存在時跳過）。"""
    if append_run is None:
        return
    try:
        append_run("backup_notion.py", ok=ok, wrote_notion=False)
    except Exception as exc:  # pragma: no cover — append_run 內部已吞例外，此為雙保險
        logger.warning("run_ledger 寫入失敗（忽略）：%s", exc)


# ── 主流程 ──────────────────────────────────────────────────────────────────


def run_backup(
    dry_run: bool = False,
    backup_root: Optional[Path] = None,
) -> Tuple[List[str], List[str]]:
    """執行全 DB 備份。

    Args:
        dry_run: True 時只列出會備份哪些 DB，不實際查詢/寫檔/推送/記帳。
        backup_root: 供測試注入的備份根目錄；``None`` 時使用 ``backups/``。

    Returns:
        ``(成功的 DB 顯示名稱清單, 失敗的 DB 顯示名稱清單)``。
    """
    logger.info("=" * 60)
    logger.info("Notion 全 DB 備份 — %s", date.today().isoformat())
    logger.info("=" * 60)

    root = backup_root if backup_root is not None else _BACKUP_ROOT
    today_str = date.today().isoformat()
    dest_dir = root / today_str

    databases = _discover_databases()
    if not databases:
        logger.error("找不到任何 *_DB_ID 環境變數，無法備份")
        return [], []

    if dry_run:
        logger.info("乾跑模式 — 將備份以下 %d 個 DB：", len(databases))
        for env_var_name in sorted(databases):
            logger.info("  - %s", _db_display_name(env_var_name))
        return [_db_display_name(k) for k in sorted(databases)], []

    notion_api_key = os.getenv("NOTION_API_KEY", "")

    succeeded: List[str] = []
    failed: List[str] = []
    for env_var_name, db_id in databases.items():
        name = _db_display_name(env_var_name)
        ok = _backup_single_db(env_var_name, db_id, notion_api_key, dest_dir)
        (succeeded if ok else failed).append(name)

    _prune_old_backups(root)

    if succeeded:
        cloud_note = _copy_to_cloud(dest_dir, today_str)
    else:
        # 全部 DB 都失敗時 dest_dir 從未被建立（見 _backup_single_db），
        # 直接嘗試複製會撲空並多印一則無意義的警告訊息。
        cloud_note = "無成功備份可同步"
    _send_summary(succeeded, failed, cloud_note)

    overall_ok = len(failed) == 0 and len(succeeded) > 0
    _log_run(ok=overall_ok)

    return succeeded, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Notion 全 DB 備份（帶日期 JSON 傾印）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出會備份哪些 DB，不實際執行"
    )
    args = parser.parse_args()
    run_backup(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
