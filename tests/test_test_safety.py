"""Regression tests for the offline test boundary; never contact a service."""

import asyncio
import os
from pathlib import Path
import socket
import sys

import pytest


def test_collection_installs_network_guard() -> None:
    # Emit the audit event directly: RED is safe even without a working guard.
    with pytest.raises(RuntimeError, match="offline tests"):
        sys.audit("socket.connect", None, ("192.0.2.1", 443))


@pytest.mark.parametrize("event", [
    "socket.getaddrinfo", "socket.sendto", "subprocess.Popen", "os.system",
])
def test_external_io_events_are_blocked(event: str) -> None:
    with pytest.raises(RuntimeError, match="offline tests"):
        sys.audit(event, None, ("192.0.2.1", 443))


def test_arbitrary_localhost_is_not_allowed() -> None:
    with pytest.raises(RuntimeError, match="offline tests"):
        sys.audit("socket.connect", None, ("127.0.0.1", 9222))


def test_dotenv_cannot_read_even_explicit_files(tmp_path: Path) -> None:
    import dotenv
    import dotenv.main

    dummy = tmp_path / "synthetic.env"
    dummy.write_text("OFFLINE_TEST_SENTINEL=not-a-secret\n", encoding="utf-8")
    assert dotenv.load_dotenv(dummy, override=True) is False
    assert dotenv.main.load_dotenv(dummy, override=True) is False
    assert dotenv.dotenv_values(dummy) == {}
    assert "OFFLINE_TEST_SENTINEL" not in os.environ


def test_config_has_no_inherited_credentials() -> None:
    import config

    assert config.settings.notion_api_key == "offline-fake-notion-key"
    assert config.settings.garmin_email == "offline@example.invalid"
    assert config.settings.garmin_password == "offline-fake-password"


@pytest.mark.parametrize("relative", [
    ".env", ".garmin_tokens/oauth1_token.json", ".garmin_web_cookies/cookies.json",
    "logs/run_ledger.jsonl", "backups/synthetic.json",
])
def test_production_files_are_protected(relative: str) -> None:
    root = Path(__file__).resolve().parent.parent
    with pytest.raises(RuntimeError, match="offline tests"):
        sys.audit("open", str(root / relative), "r", 0)


def test_runtime_paths_are_redirected() -> None:
    from scripts import backup_notion, daily_adjust, health_tracker, telegram_bot
    from utils import adherence, logger, run_ledger

    root = Path(__file__).resolve().parent.parent
    paths = [logger._LOG_DIR, run_ledger._RUN_LEDGER_PATH,
             adherence._ADVICE_LOG_PATH, daily_adjust._ADVICE_LOG,
             telegram_bot._RPE_PENDING_LOG, backup_notion._BACKUP_ROOT,
             health_tracker._GARMIN_TOKEN_DIR]
    for path in paths:
        assert root not in Path(path).parents
        assert "lifeos-offline-" in str(path)


def test_socketpair_and_asyncio_still_work() -> None:
    left, right = socket.socketpair()
    try:
        left.sendall(b"ok")
        assert right.recv(2) == b"ok"
    finally:
        left.close()
        right.close()

    async def answer() -> int:
        return 42

    assert asyncio.run(answer()) == 42


def test_http_transport_is_blocked_before_dns() -> None:
    import requests

    with pytest.raises(RuntimeError, match="offline tests"):
        requests.Session().request("GET", "https://example.invalid")


def test_native_curl_transport_is_blocked() -> None:
    from curl_cffi import requests

    with requests.Session() as session:
        with pytest.raises(RuntimeError, match="offline tests"):
            session.request("GET", "https://example.invalid")


def test_httpx_sync_and_async_are_blocked() -> None:
    import httpx

    with httpx.Client() as client:
        with pytest.raises(RuntimeError, match="offline tests"):
            client.send(httpx.Request("GET", "https://example.invalid"))

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(RuntimeError, match="offline tests"):
                await client.send(httpx.Request("GET", "https://example.invalid"))

    asyncio.run(request())


@pytest.mark.parametrize("event", ["os.remove", "os.rmdir", "os.mkdir", "os.listdir", "os.scandir"])
def test_production_mutation_and_listing_events_are_blocked(event: str) -> None:
    root = Path(__file__).resolve().parent.parent
    with pytest.raises(RuntimeError, match="offline tests"):
        sys.audit(event, str(root / "logs"))


@pytest.mark.parametrize("event", ["os.rename", "os.link", "os.symlink"])
def test_production_move_or_link_events_are_blocked(event: str) -> None:
    root = Path(__file__).resolve().parent.parent
    with pytest.raises(RuntimeError, match="offline tests"):
        sys.audit(event, "synthetic-source", str(root / ".env"))


def test_guard_installation_is_idempotent() -> None:
    from scripts.test_offline import install_guard

    before = socket.socketpair
    assert install_guard() == install_guard()
    assert socket.socketpair is before
