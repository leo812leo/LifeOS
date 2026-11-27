"""Offline regressions for Notion create/update API error handling.

Run explicitly with:
    python scripts/test_offline.py tests/test_notion_api_errors.py -q

All payloads are synthetic. Passing these tests does not establish the cause of
any original production API rejection or prove that activity sync has recovered.
"""

import logging
from typing import cast
from unittest.mock import MagicMock

import httpx
from notion_client import APIErrorCode, APIResponseError
from notion_client.client import BaseClient
import pytest

from notion_client_helper import create_page, update_page


_FAKE_MESSAGE = "synthetic-message-do-not-log"
_FAKE_BODY = "synthetic-body-do-not-log"
_FAKE_INVALID_CODE = "synthetic-invalid-code-do-not-log"


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "error_code,expected_code",
    [
        (APIErrorCode.ValidationError, "validation_error"),
        (APIErrorCode.RateLimited, "rate_limited"),
        ("validation_error", "validation_error"),
        ("rate_limited", "rate_limited"),
        (_FAKE_INVALID_CODE, "unknown"),
    ],
    ids=[
        "enum-validation",
        "enum-rate-limited",
        "sdk-raw-validation",
        "sdk-raw-rate-limited",
        "invalid-code",
    ],
)
def test_api_error_returns_none_without_sensitive_logging(
    operation: str,
    error_code: object,
    expected_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real SDK exceptions must not escape or reflect message/body/raw codes."""
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://synthetic.invalid/v1/pages"),
        json={"code": error_code, "message": _FAKE_MESSAGE, "private_detail": _FAKE_BODY},
    )
    if type(error_code) is str and expected_code != "unknown":
        with pytest.raises(APIResponseError) as parsed:
            BaseClient._parse_response(MagicMock(), response)
        error = parsed.value
        assert type(error.code) is str
    else:
        error = APIResponseError(
            response,
            message=_FAKE_MESSAGE,
            code=cast(APIErrorCode, error_code),
        )
    client = MagicMock()
    mock_write = getattr(client.pages, operation)
    mock_write.side_effect = error
    helper = create_page if operation == "create" else update_page

    with caplog.at_level(logging.ERROR, logger="notion_client_helper"):
        result = helper(
            client,
            "synthetic-target-id",
            {"Name": {"title": [{"text": {"content": "synthetic-content"}}]}},
        )

    assert result is None
    mock_write.assert_called_once()
    assert "Notion API 錯誤：" + expected_code in caplog.text
    assert _FAKE_MESSAGE not in caplog.text
    assert _FAKE_BODY not in caplog.text
    assert _FAKE_INVALID_CODE not in caplog.text
