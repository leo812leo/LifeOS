"""tests/test_notion_helpers.py — notion_client_helper 屬性格式化函式與查詢函式的單元測試。

這些函式是純函式（property formatters）或透過 mock 測試 Notion API 互動。
"""

from unittest.mock import MagicMock, patch

import pytest

from notion_client_helper import (
    NotionQueryError,
    get_latest_record,
    page_exists_for_date,
    prop_date,
    prop_number,
    prop_rich_text,
    prop_select,
    prop_title,
    query_all_pages,
    query_pages,
    update_page,
)


# ── prop_title ─────────────────────────────────────────────────────────────────


class TestPropTitle:
    def test_basic(self) -> None:
        result = prop_title("Hello")
        assert result == {"title": [{"text": {"content": "Hello"}}]}

    def test_converts_to_str(self) -> None:
        result = prop_title(123)  # type: ignore[arg-type]
        assert result["title"][0]["text"]["content"] == "123"

    def test_empty_string(self) -> None:
        result = prop_title("")
        assert result["title"][0]["text"]["content"] == ""


# ── prop_number ────────────────────────────────────────────────────────────────


class TestPropNumber:
    def test_rounds_to_two_decimals(self) -> None:
        result = prop_number(3.14159)
        assert result == {"number": 3.14}

    def test_integer(self) -> None:
        result = prop_number(100.0)
        assert result == {"number": 100.0}

    def test_zero(self) -> None:
        result = prop_number(0.0)
        assert result == {"number": 0.0}

    def test_large_value(self) -> None:
        result = prop_number(1_234_567.891)
        assert result == {"number": 1_234_567.89}


# ── prop_date ──────────────────────────────────────────────────────────────────


class TestPropDate:
    def test_basic(self) -> None:
        result = prop_date("2024-01-15")
        assert result == {"date": {"start": "2024-01-15"}}

    def test_preserves_string(self) -> None:
        date_str = "2025-12-31"
        result = prop_date(date_str)
        assert result["date"]["start"] == date_str


# ── prop_select ────────────────────────────────────────────────────────────────


class TestPropSelect:
    def test_basic(self) -> None:
        result = prop_select("Option A")
        assert result == {"select": {"name": "Option A"}}

    def test_converts_to_str(self) -> None:
        result = prop_select(42)  # type: ignore[arg-type]
        assert result["select"]["name"] == "42"


# ── prop_rich_text ─────────────────────────────────────────────────────────────


class TestPropRichText:
    def test_basic(self) -> None:
        result = prop_rich_text("some text")
        assert result == {"rich_text": [{"text": {"content": "some text"}}]}

    def test_empty_string(self) -> None:
        result = prop_rich_text("")
        assert result["rich_text"][0]["text"]["content"] == ""

    def test_converts_to_str(self) -> None:
        result = prop_rich_text(99)  # type: ignore[arg-type]
        assert result["rich_text"][0]["text"]["content"] == "99"


# ── update_page ────────────────────────────────────────────────────────────────


class TestUpdatePage:
    """update_page() 用 client.pages.update() 對既有頁面做部分更新。"""

    def test_returns_response_on_success(self) -> None:
        client = MagicMock()
        client.pages.update.return_value = {"id": "page-001"}
        result = update_page(client, "page-001", {"RPE": prop_number(7)})
        assert result == {"id": "page-001"}

    def test_passes_page_id_and_properties(self) -> None:
        client = MagicMock()
        client.pages.update.return_value = {"id": "page-001"}
        props = {"RPE": prop_number(7), "Notes": prop_rich_text("小腿緊")}
        update_page(client, "page-001", props)
        client.pages.update.assert_called_once_with(
            page_id="page-001", properties=props
        )

    def test_returns_none_on_object_not_found(self) -> None:
        from notion_client import APIErrorCode, APIResponseError

        client = MagicMock()
        client.pages.update.side_effect = APIResponseError(
            MagicMock(), "not found", APIErrorCode.ObjectNotFound
        )
        result = update_page(client, "missing-page", {"RPE": prop_number(5)})
        assert result is None

    def test_returns_none_on_unauthorized(self) -> None:
        from notion_client import APIErrorCode, APIResponseError

        client = MagicMock()
        client.pages.update.side_effect = APIResponseError(
            MagicMock(), "unauthorized", APIErrorCode.Unauthorized
        )
        result = update_page(client, "page-001", {"RPE": prop_number(5)})
        assert result is None

    def test_returns_none_on_unexpected_exception(self) -> None:
        client = MagicMock()
        client.pages.update.side_effect = Exception("network error")
        result = update_page(client, "page-001", {"RPE": prop_number(5)})
        assert result is None


# ── query_pages ────────────────────────────────────────────────────────────────


class TestQueryPages:
    """query_pages() 使用 client.request() 呼叫 Notion API（notion-client 2.7+）。"""

    def test_returns_results_on_success(self) -> None:
        client = MagicMock()
        client.request.return_value = {
            "results": [{"id": "page-001"}, {"id": "page-002"}]
        }
        results = query_pages(client, "db-123")
        assert len(results) == 2
        assert results[0]["id"] == "page-001"

    def test_passes_filter_and_sorts(self) -> None:
        client = MagicMock()
        client.request.return_value = {"results": []}
        filter_dict = {"property": "Date", "date": {"equals": "2024-01-15"}}
        sorts = [{"property": "Date", "direction": "descending"}]

        query_pages(client, "db-123", filter_dict=filter_dict, sorts=sorts, page_size=5)

        client.request.assert_called_once_with(
            path="databases/db-123/query",
            method="POST",
            body={
                "page_size": 5,
                "filter": filter_dict,
                "sorts": sorts,
            },
        )

    def test_returns_empty_on_exception(self) -> None:
        client = MagicMock()
        client.request.side_effect = Exception("network error")
        results = query_pages(client, "db-123")
        assert results == []

    def test_omits_filter_when_none(self) -> None:
        """filter_dict=None 時不應把 filter 鍵傳給 API。"""
        client = MagicMock()
        client.request.return_value = {"results": []}
        query_pages(client, "db-123", filter_dict=None, sorts=None)
        call_kwargs = client.request.call_args[1]
        body = call_kwargs["body"]
        assert "filter" not in body
        assert "sorts" not in body

    def test_raise_on_error_false_by_default_returns_empty(self) -> None:
        """raise_on_error 預設 False，維持舊行為（回傳空清單，不拋例外）。"""
        client = MagicMock()
        client.request.side_effect = Exception("network error")
        results = query_pages(client, "db-123")
        assert results == []

    def test_raises_notion_query_error_on_api_exception(self) -> None:
        """raise_on_error=True 時，一般例外應轉為 NotionQueryError 拋出。"""
        client = MagicMock()
        client.request.side_effect = Exception("network error")
        with pytest.raises(NotionQueryError):
            query_pages(client, "db-123", raise_on_error=True)

    def test_raises_notion_query_error_on_api_response_error(self) -> None:
        """raise_on_error=True 時，APIResponseError 也應轉為 NotionQueryError 拋出。"""
        from notion_client import APIErrorCode, APIResponseError

        client = MagicMock()
        client.request.side_effect = APIResponseError(
            MagicMock(), "internal error", APIErrorCode.InternalServerError
        )
        with pytest.raises(NotionQueryError):
            query_pages(client, "db-123", raise_on_error=True)


# ── query_all_pages ────────────────────────────────────────────────────────────


class TestQueryAllPages:
    """query_all_pages() 用 start_cursor 迴圈直到 has_more=False（T14 備份用途）。"""

    def test_single_page_no_pagination_needed(self) -> None:
        client = MagicMock()
        client.request.return_value = {
            "results": [{"id": "page-001"}, {"id": "page-002"}],
            "has_more": False,
        }
        results = query_all_pages(client, "db-123")
        assert len(results) == 2
        assert client.request.call_count == 1

    def test_follows_pagination_across_two_pages(self) -> None:
        """驗收要求：mock 兩頁資料，斷言全量彙整。"""
        client = MagicMock()
        client.request.side_effect = [
            {
                "results": [{"id": "page-001"}, {"id": "page-002"}],
                "has_more": True,
                "next_cursor": "cursor-abc",
            },
            {
                "results": [{"id": "page-003"}],
                "has_more": False,
                "next_cursor": None,
            },
        ]
        results = query_all_pages(client, "db-123")

        assert client.request.call_count == 2
        assert [p["id"] for p in results] == ["page-001", "page-002", "page-003"]

        second_call_body = client.request.call_args_list[1][1]["body"]
        assert second_call_body["start_cursor"] == "cursor-abc"

    def test_first_call_omits_start_cursor(self) -> None:
        client = MagicMock()
        client.request.return_value = {"results": [], "has_more": False}
        query_all_pages(client, "db-123")

        first_call_body = client.request.call_args_list[0][1]["body"]
        assert "start_cursor" not in first_call_body

    def test_stops_if_has_more_true_but_no_cursor(self) -> None:
        """防呆：has_more=True 但沒給 next_cursor 時不應無限迴圈。"""
        client = MagicMock()
        client.request.return_value = {
            "results": [{"id": "page-001"}],
            "has_more": True,
            "next_cursor": None,
        }
        results = query_all_pages(client, "db-123")
        assert len(results) == 1
        assert client.request.call_count == 1

    def test_passes_filter_and_sorts_on_every_page(self) -> None:
        client = MagicMock()
        client.request.side_effect = [
            {"results": [{"id": "p1"}], "has_more": True, "next_cursor": "c1"},
            {"results": [{"id": "p2"}], "has_more": False},
        ]
        filter_dict = {"property": "Date", "date": {"is_not_empty": True}}
        sorts = [{"property": "Date", "direction": "ascending"}]

        query_all_pages(client, "db-123", filter_dict=filter_dict, sorts=sorts)

        for call in client.request.call_args_list:
            body = call[1]["body"]
            assert body["filter"] == filter_dict
            assert body["sorts"] == sorts

    def test_returns_empty_on_exception_when_not_raising(self) -> None:
        client = MagicMock()
        client.request.side_effect = Exception("network error")
        results = query_all_pages(client, "db-123")
        assert results == []

    def test_returns_partial_results_collected_before_failure(self) -> None:
        """分頁途中失敗時，回傳已收集到的（不完整）頁面，而非整批作廢。"""
        client = MagicMock()
        client.request.side_effect = [
            {"results": [{"id": "page-001"}], "has_more": True, "next_cursor": "c1"},
            Exception("network error"),
        ]
        results = query_all_pages(client, "db-123")
        assert [p["id"] for p in results] == ["page-001"]

    def test_raises_notion_query_error_when_raise_on_error_true(self) -> None:
        client = MagicMock()
        client.request.side_effect = Exception("network error")
        with pytest.raises(NotionQueryError):
            query_all_pages(client, "db-123", raise_on_error=True)

    def test_raises_notion_query_error_on_api_response_error(self) -> None:
        from notion_client import APIErrorCode, APIResponseError

        client = MagicMock()
        client.request.side_effect = APIResponseError(
            MagicMock(), "internal error", APIErrorCode.InternalServerError
        )
        with pytest.raises(NotionQueryError):
            query_all_pages(client, "db-123", raise_on_error=True)

    def test_uses_page_size_100_by_default(self) -> None:
        client = MagicMock()
        client.request.return_value = {"results": [], "has_more": False}
        query_all_pages(client, "db-123")
        body = client.request.call_args[1]["body"]
        assert body["page_size"] == 100


# ── page_exists_for_date ───────────────────────────────────────────────────────


class TestPageExistsForDate:
    def test_returns_true_when_page_exists(self) -> None:
        with patch(
            "notion_client_helper.query_pages", return_value=[{"id": "page-001"}]
        ):
            result = page_exists_for_date(MagicMock(), "db-123", "2024-01-15")
        assert result is True

    def test_returns_false_when_no_pages(self) -> None:
        with patch("notion_client_helper.query_pages", return_value=[]):
            result = page_exists_for_date(MagicMock(), "db-123", "2024-01-15")
        assert result is False

    def test_passes_correct_filter(self) -> None:
        """應傳入正確的 date equals filter。"""
        with patch(
            "notion_client_helper.query_pages", return_value=[]
        ) as mock_query:
            page_exists_for_date(MagicMock(), "db-123", "2024-01-15", date_property="My Date")

        call_kwargs = mock_query.call_args
        filter_arg = call_kwargs[1].get("filter_dict") or call_kwargs[0][2]
        assert filter_arg["property"] == "My Date"
        assert filter_arg["date"]["equals"] == "2024-01-15"

    def test_defaults_to_not_raising(self) -> None:
        """raise_on_error 預設 False：底層 query_pages 失敗時回傳 []，不拋例外。"""
        client = MagicMock()
        client.request.side_effect = Exception("network error")
        result = page_exists_for_date(client, "db-123", "2024-01-15")
        assert result is False

    def test_raise_on_error_propagates_notion_query_error(self) -> None:
        """raise_on_error=True 時，查詢失敗應讓 NotionQueryError 往上傳，
        而不是被吞掉回傳 False（避免 dedup 把查詢失敗誤判為「今天沒寫過」）。"""
        client = MagicMock()
        client.request.side_effect = Exception("network error")
        with pytest.raises(NotionQueryError):
            page_exists_for_date(client, "db-123", "2024-01-15", raise_on_error=True)


# ── get_latest_record ──────────────────────────────────────────────────────────


class TestGetLatestRecord:
    def test_returns_first_result(self) -> None:
        with patch(
            "notion_client_helper.query_pages",
            return_value=[{"id": "latest"}, {"id": "older"}],
        ):
            result = get_latest_record(MagicMock(), "db-123")
        assert result is not None
        assert result["id"] == "latest"

    def test_returns_none_when_empty(self) -> None:
        with patch("notion_client_helper.query_pages", return_value=[]):
            result = get_latest_record(MagicMock(), "db-123")
        assert result is None

    def test_passes_descending_sort(self) -> None:
        """應傳入 descending 排序讓最新的在第一筆。"""
        with patch(
            "notion_client_helper.query_pages", return_value=[]
        ) as mock_query:
            get_latest_record(MagicMock(), "db-123", date_property="Date")

        call_kwargs = mock_query.call_args
        sorts_arg = call_kwargs[1].get("sorts") or call_kwargs[0][3]
        assert sorts_arg[0]["direction"] == "descending"
        assert sorts_arg[0]["property"] == "Date"
