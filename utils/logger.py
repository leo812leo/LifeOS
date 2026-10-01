"""utils/logger.py — 統一日誌工具。

每個模組呼叫 ``get_logger(__name__)`` 取得 logger。
日誌同時輸出到終端機（INFO+）和 ``logs/YYYY-MM-DD.log``（DEBUG+）。

使用方式::

    from utils.logger import get_logger

    logger = get_logger(__name__)
    logger.info("腳本啟動")
    logger.error("發生錯誤：%s", err)
"""

import logging
from datetime import date
from pathlib import Path

# 日誌資料夾（專案根目錄下的 logs/）
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """取得已設定好的 logger。

    相同 ``name`` 在同一 process 中只會初始化一次（handler 不重複加入）。

    Args:
        name: 通常傳入 ``__name__``，例如 ``'scripts.investment_tracker'``。

    Returns:
        設定完成的 :class:`logging.Logger` 物件。
    """
    logger = logging.getLogger(name)

    # 避免重複加入 handler（多次 import 時會觸發）
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # ── 終端機輸出（INFO 以上）────────────────────────────────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ── 檔案輸出（DEBUG 以上，含詳細堆疊）──────────────────────────────────
    _LOG_DIR.mkdir(exist_ok=True)
    log_file = _LOG_DIR / f"{date.today().isoformat()}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
