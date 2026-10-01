"""notion_client_helper.py — Notion API 封裝。

提供讀寫 Notion Database 的統一介面，由各追蹤腳本共用。

使用方式::

    from notion_client_helper import get_notion_client, create_page, prop_title

    client = get_notion_client(api_key)
    result = create_page(client, database_id, properties)
"""

from typing import Dict, List, Optional

from notion_client import APIErrorCode, APIResponseError, Client

from utils.logger import get_logger

logger = get_logger(__name__)


# ── 例外 ──────────────────────────────────────────────────────────────────────


class NotionQueryError(Exception):
    """Notion 查詢失敗（區別於「查無資料」）。

    ``query_pages`` 預設在查詢失敗時回傳 ``[]``，與「查無符合條件的頁面」無法區分。
    對於防重複寫入（dedup）等場景，這個混淆很危險：查詢失敗被誤判為「今天還沒寫過」，
    會導致腳本在 Notion 暫時性錯誤時繼續寫入，造成重複記錄。

    呼叫端可傳入 ``raise_on_error=True`` 改用這個例外，以便在查詢失敗時選擇「不寫」
    而非「當作沒寫過繼續寫」。
    """


# ── Client ────────────────────────────────────────────────────────────────────


def get_notion_client(api_key: str) -> Client:
    """建立並回傳 Notion Client 物件。

    使用 Notion API 版本 2022-06-28 以確保 databases/query endpoint 可用。

    Args:
        api_key: Notion Integration Token（``NOTION_API_KEY``）。

    Returns:
        已初始化的 :class:`notion_client.Client` 物件。
    """
    return Client(auth=api_key, notion_version="2022-06-28")


# ── Page CRUD ─────────────────────────────────────────────────────────────────


def create_page(
    client: Client,
    database_id: str,
    properties: Dict[str, object],
) -> Optional[Dict]:
    """在指定 Database 新增一筆記錄（page）。

    Args:
        client: Notion Client 物件。
        database_id: 目標 Database ID。
        properties: 符合 Notion API 格式的 properties dict。

    Returns:
        成功時回傳 API response dict；失敗時回傳 None。
    """
    try:
        response = client.pages.create(
            parent={"database_id": database_id},
            properties=properties,
        )
        logger.info("Notion 寫入成功，page_id=%s", response["id"])
        return response

    except APIResponseError as exc:
        if exc.code == APIErrorCode.ObjectNotFound:
            logger.error(
                "找不到 Database（ID: %s），請確認 DB ID 與分享設定。",
                database_id,
            )
        elif exc.code == APIErrorCode.Unauthorized:
            logger.error("Notion API Key 無效或已過期，請重新確認 .env 設定。")
        else:
            logger.error("Notion API 錯誤：%s - %s", exc.code, exc.message)
        return None

    except Exception as exc:
        logger.error("寫入 Notion 時發生未預期錯誤：%s", exc)
        return None


def update_page(
    client: Client,
    page_id: str,
    properties: Dict[str, object],
) -> Optional[Dict]:
    """更新既有頁面（page）的 properties。

    只更新 ``properties`` 中列出的欄位，其餘既有欄位不受影響（Notion API
    的 ``pages.update`` 是部分更新，不是整頁覆蓋）。

    Args:
        client: Notion Client 物件。
        page_id: 目標頁面 ID。
        properties: 符合 Notion API 格式的 properties dict（只需包含要更新的欄位）。

    Returns:
        成功時回傳 API response dict；失敗時回傳 None。
    """
    try:
        response = client.pages.update(page_id=page_id, properties=properties)
        logger.info("Notion 更新成功，page_id=%s", response["id"])
        return response

    except APIResponseError as exc:
        if exc.code == APIErrorCode.ObjectNotFound:
            logger.error("找不到頁面（ID: %s），可能已被刪除或分享權限不足。", page_id)
        elif exc.code == APIErrorCode.Unauthorized:
            logger.error("Notion API Key 無效或已過期，請重新確認 .env 設定。")
        else:
            logger.error("Notion API 錯誤：%s - %s", exc.code, exc.message)
        return None

    except Exception as exc:
        logger.error("更新 Notion 頁面時發生未預期錯誤：%s", exc)
        return None


def query_pages(
    client: Client,
    database_id: str,
    filter_dict: Optional[Dict] = None,
    sorts: Optional[List[Dict]] = None,
    page_size: int = 10,
    raise_on_error: bool = False,
) -> List[Dict]:
    """查詢 Database 中的頁面。

    Args:
        client: Notion Client 物件。
        database_id: 目標 Database ID。
        filter_dict: Notion filter 條件（可選）。
        sorts: 排序規則清單（可選）。
        page_size: 最多回傳筆數（預設 10）。
        raise_on_error: True 時查詢失敗改為拋出 :class:`NotionQueryError`，
            讓呼叫端能區分「查詢失敗」與「查無資料」（預設 False，維持舊行為）。

    Returns:
        符合條件的頁面 dict 清單；查詢失敗且 ``raise_on_error=False`` 時回傳空清單。

    Raises:
        NotionQueryError: 查詢失敗且 ``raise_on_error=True`` 時拋出。
    """
    try:
        kwargs: Dict[str, object] = {
            "database_id": database_id,
            "page_size": page_size,
        }
        if filter_dict is not None:
            kwargs["filter"] = filter_dict
        if sorts is not None:
            kwargs["sorts"] = sorts

        # notion-client 2.7+ 移除了 databases.query()，改用 request() 直接呼叫
        body: Dict[str, object] = {"page_size": page_size}
        if filter_dict is not None:
            body["filter"] = filter_dict
        if sorts is not None:
            body["sorts"] = sorts

        response = client.request(
            path=f"databases/{database_id}/query",
            method="POST",
            body=body,
        )
        return response.get("results", [])

    except APIResponseError as exc:
        logger.error("Notion query 失敗：%s - %s", exc.code, str(exc))
        if raise_on_error:
            raise NotionQueryError(str(exc)) from exc
        return []

    except Exception as exc:
        logger.error("Notion query 發生未預期錯誤：%s", exc)
        if raise_on_error:
            raise NotionQueryError(str(exc)) from exc
        return []


def query_all_pages(
    client: Client,
    database_id: str,
    filter_dict: Optional[Dict] = None,
    sorts: Optional[List[Dict]] = None,
    page_size: int = 100,
    raise_on_error: bool = False,
) -> List[Dict]:
    """查詢 Database 中**全部**符合條件的頁面（自動處理分頁）。

    ``query_pages`` 有 ``page_size`` 上限（預設 10）且不處理 ``start_cursor``，
    對於需要完整資料集的場景（例如 :mod:`scripts.backup_notion` 全庫備份）會
    silent 截斷。此函式改用 ``start_cursor`` 迴圈，直到 Notion 回傳
    ``has_more=False`` 為止才停止，彙整所有分頁後一次回傳。

    刻意獨立成新函式而非修改 ``query_pages`` 的預設行為：現有呼叫端（防重複
    寫入、儀表板單筆查詢等）依賴「最多回傳 page_size 筆」這個既有行為，改成
    全量拉取可能在既有呼叫路徑上造成非預期的效能或邏輯影響。

    Args:
        client: Notion Client 物件。
        database_id: 目標 Database ID。
        filter_dict: Notion filter 條件（可選）。
        sorts: 排序規則清單（可選）。
        page_size: 每次 API 呼叫的筆數上限（預設 100，Notion API 上限）。
        raise_on_error: True 時查詢失敗改為拋出 :class:`NotionQueryError`，
            而非回傳目前已收集到的頁面（預設 False）。全量備份等需要「不完整
            就視為失敗」語意的呼叫端應設為 True。

    Returns:
        該 Database 所有符合條件的頁面 dict 清單（依 Notion 回傳順序彙整）。
        ``raise_on_error=False`` 時，若查詢在分頁途中失敗，回傳當下已收集到
        的（不完整的）頁面清單，而非整個作廢。

    Raises:
        NotionQueryError: 查詢失敗且 ``raise_on_error=True`` 時拋出。
    """
    all_pages: List[Dict] = []
    start_cursor: Optional[str] = None

    while True:
        body: Dict[str, object] = {"page_size": page_size}
        if filter_dict is not None:
            body["filter"] = filter_dict
        if sorts is not None:
            body["sorts"] = sorts
        if start_cursor is not None:
            body["start_cursor"] = start_cursor

        try:
            response = client.request(
                path=f"databases/{database_id}/query",
                method="POST",
                body=body,
            )
        except APIResponseError as exc:
            logger.error("Notion query_all_pages 失敗：%s - %s", exc.code, str(exc))
            if raise_on_error:
                raise NotionQueryError(str(exc)) from exc
            return all_pages

        except Exception as exc:
            logger.error("Notion query_all_pages 發生未預期錯誤：%s", exc)
            if raise_on_error:
                raise NotionQueryError(str(exc)) from exc
            return all_pages

        all_pages.extend(response.get("results", []))

        if not response.get("has_more"):
            break
        start_cursor = response.get("next_cursor")
        if not start_cursor:
            break

    return all_pages


def page_exists_for_date(
    client: Client,
    database_id: str,
    date_str: str,
    date_property: str = "Date",
    raise_on_error: bool = False,
) -> bool:
    """檢查指定日期的記錄是否已存在（防重複寫入）。

    Args:
        client: Notion Client 物件。
        database_id: 目標 Database ID。
        date_str: ISO 格式日期字串，例如 ``'2024-01-15'``。
        date_property: Notion 日期欄位名稱（預設 ``'Date'``）。
        raise_on_error: True 時查詢失敗改為拋出 :class:`NotionQueryError`，
            而非回傳 ``False``（預設 False，維持舊行為）。呼叫端用於 dedup
            檢查時應設為 True：查詢失敗與「今天沒寫過」意義不同，混淆會導致
            Notion 暫時性錯誤時仍繼續寫入而造成重複記錄。

    Returns:
        該日期已有記錄時回傳 True，否則回傳 False。

    Raises:
        NotionQueryError: 查詢失敗且 ``raise_on_error=True`` 時拋出。
    """
    filter_dict = {
        "property": date_property,
        "date": {"equals": date_str},
    }
    pages = query_pages(
        client,
        database_id,
        filter_dict=filter_dict,
        page_size=1,
        raise_on_error=raise_on_error,
    )
    return len(pages) > 0


def get_latest_record(
    client: Client,
    database_id: str,
    date_property: str = "Date",
) -> Optional[Dict]:
    """取得最新一筆記錄（依日期由新到舊排序，取第一筆）。

    Args:
        client: Notion Client 物件。
        database_id: 目標 Database ID。
        date_property: 用於排序的日期欄位名稱（預設 ``'Date'``）。

    Returns:
        最新一筆頁面 dict；查無資料時回傳 None。
    """
    sorts = [{"property": date_property, "direction": "descending"}]
    pages = query_pages(client, database_id, sorts=sorts, page_size=1)
    return pages[0] if pages else None


# ── 屬性格式化輔助函式 ─────────────────────────────────────────────────────────


def prop_title(text: str) -> Dict:
    """建立 title 類型屬性。

    Args:
        text: 標題文字。

    Returns:
        Notion ``title`` property dict。
    """
    return {"title": [{"text": {"content": str(text)}}]}


def prop_number(value: float) -> Dict:
    """建立 number 類型屬性（四捨五入至小數點後兩位）。

    Args:
        value: 數值。

    Returns:
        Notion ``number`` property dict。
    """
    return {"number": round(value, 2)}


def prop_date(date_str: str) -> Dict:
    """建立 date 類型屬性。

    Args:
        date_str: ISO 格式日期字串，例如 ``'2024-01-15'``。

    Returns:
        Notion ``date`` property dict。
    """
    return {"date": {"start": date_str}}


def prop_select(option: str) -> Dict:
    """建立 select 類型屬性。

    Args:
        option: 選項名稱。

    Returns:
        Notion ``select`` property dict。
    """
    return {"select": {"name": str(option)}}


def prop_rich_text(text: str) -> Dict:
    """建立 rich_text 類型屬性。

    Args:
        text: 文字內容。

    Returns:
        Notion ``rich_text`` property dict。
    """
    return {"rich_text": [{"text": {"content": str(text)}}]}
