"""conftest.py — pytest 共用 fixtures。"""

import sys
import os
import platform
import socket
import threading
from pathlib import Path

import pytest
import dotenv

# Cache harmless OS discovery before blocking subprocesses. On Windows,
# pandas uses platform.uname(), which may otherwise invoke `cmd /c ver`.
platform.uname()

# Install before test-module imports: no parent .env, inherited credentials,
# subprocesses or live network traffic may be used by the public test suite.
dotenv.load_dotenv = lambda *args, **kwargs: False
for _name in list(os.environ):
    if any(part in _name.upper() for part in (
        "NOTION", "GARMIN", "GARTH", "TELEGRAM", "ANTHROPIC", "OPENROUTER",
        "SHIOAJI", "SCHWAB", "API_KEY", "TOKEN", "PASSWORD", "SECRET",
    )):
        os.environ.pop(_name, None)


_socketpair_state = threading.local()
_original_socketpair = socket.socketpair


def _local_socketpair(*args, **kwargs):
    # Windows implements socketpair with a temporary loopback connection.
    # Permit only that stdlib operation, not arbitrary localhost services.
    _socketpair_state.active = True
    try:
        return _original_socketpair(*args, **kwargs)
    finally:
        _socketpair_state.active = False


socket.socketpair = _local_socketpair


def _deny_external_io(event, args):
    if event == "socket.connect" and getattr(_socketpair_state, "active", False):
        if isinstance(args[1], tuple) and args[1][0] in {"127.0.0.1", "::1"}:
            return
    if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
        raise RuntimeError("Live network and subprocess access are forbidden in tests")


sys.addaudithook(_deny_external_io)

# 確保專案根目錄在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
