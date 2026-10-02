"""Run isolated tests without production credentials, services or runtime files.

Usage: python scripts/test_offline.py [pytest arguments]

The guard is installed before importing pytest. tests/conftest.py also installs
it for direct pytest runs before test collection. This is protection against
accidental integration calls, not a sandbox for untrusted/native code.
"""

import atexit
import importlib.abc
import importlib.machinery
import logging
import os
from pathlib import Path
import platform
import socket
import sys
import tempfile
import threading
from types import ModuleType
from typing import Any, Dict, List, Optional


_ROOT = Path(__file__).resolve().parent.parent
_STATE = "_lifeos_offline_test_root"
_MESSAGE = "External IO and production files are forbidden in offline tests"
_SOCKETPAIR_STATE = threading.local()
_ORIGINAL_SOCKETPAIR = socket.socketpair
_PROTECTED = tuple(os.path.normcase(str(_ROOT / name)) for name in (
    "logs", "backups", ".garmin_tokens", ".garth_tokens", ".garmin_web_cookies",
)) + tuple(os.path.normcase(str(Path.home() / name)) for name in (
    "OneDrive", "Google Drive", "GoogleDrive", "Dropbox",
))
_TARGETS = {
    "dotenv", "dotenv.main", "requests.sessions", "httpx",
    "curl_cffi.requests.session", "utils.logger", "utils.run_ledger",
    "utils.adherence", "scripts.daily_adjust", "scripts.telegram_bot",
    "scripts.backup_notion", "scripts.health_tracker", "scripts.training_advisor",
    "scripts.health_tracker_web",
}


def _blocked(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(_MESSAGE)


async def _blocked_async(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(_MESSAGE)


def _protected_path(value: Any) -> bool:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return False
    path = os.path.normcase(os.path.abspath(os.fsdecode(value)))
    name = os.path.basename(path)
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return any(path == root or path.startswith(root + os.sep) for root in _PROTECTED)


def _deny_external_io(event: str, args: tuple) -> None:
    if event == "socket.connect" and getattr(_SOCKETPAIR_STATE, "active", False):
        address = args[1]
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return
    if event in {
        "socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
        "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
    }:
        _blocked()
    if event in {"open", "os.remove", "os.rmdir", "os.mkdir", "os.listdir", "os.scandir"}:
        if args and _protected_path(args[0]):
            _blocked()
    if event in {"os.rename", "os.link", "os.symlink"}:
        if any(_protected_path(path) for path in args[:2]):
            _blocked()


def _local_socketpair(*args: Any, **kwargs: Any) -> Any:
    # Windows implements socketpair with a loopback connection. Only this
    # stdlib operation is allowed, not arbitrary local services or browsers.
    previous = getattr(_SOCKETPAIR_STATE, "active", False)
    _SOCKETPAIR_STATE.active = True
    try:
        return _ORIGINAL_SOCKETPAIR(*args, **kwargs)
    finally:
        _SOCKETPAIR_STATE.active = previous


def _apply_module_safety(module: ModuleType) -> None:
    root = Path(getattr(sys, _STATE))
    redirects: Dict[str, Dict[str, Any]] = {
        "utils.logger": {"_LOG_DIR": root / "logs"},
        "utils.run_ledger": {"_RUN_LEDGER_PATH": root / "logs/run_ledger.jsonl"},
        "utils.adherence": {"_ADVICE_LOG_PATH": root / "logs/advice_log.jsonl"},
        "scripts.daily_adjust": {"_ADVICE_LOG": root / "logs/advice_log.jsonl"},
        "scripts.telegram_bot": {"_RPE_PENDING_LOG": root / "logs/rpe_pending.jsonl"},
        "scripts.backup_notion": {"_BACKUP_ROOT": root / "backups"},
        "scripts.health_tracker": {"_GARMIN_TOKEN_DIR": root / "garmin_tokens"},
        "scripts.training_advisor": {
            "_GARMIN_TOKEN_DIR": str(root / "garmin_tokens"), "_REPO_ROOT": root,
        },
        "scripts.health_tracker_web": {
            "_COOKIE_DIR": root / "garmin_web_cookies",
            "_COOKIE_FILE": root / "garmin_web_cookies/cookies.json",
            "_TOKEN_CACHE_FILE": root / "garmin_web_cookies/oauth_token.json",
        },
    }
    for name, value in redirects.get(module.__name__, {}).items():
        setattr(module, name, value)
    if module.__name__ in {"dotenv", "dotenv.main"}:
        module.load_dotenv = lambda *args, **kwargs: False
        module.dotenv_values = lambda *args, **kwargs: {}
    if module.__name__ == "requests.sessions":
        module.Session.request = _blocked
    if module.__name__ == "httpx":
        module.Client.send = _blocked
        module.AsyncClient.send = _blocked_async
    if module.__name__ == "curl_cffi.requests.session":
        module.Session.request = _blocked
        module.AsyncSession.request = _blocked_async


class _SafeLoader(importlib.abc.Loader):
    def __init__(self, original: Any) -> None:
        self.original = original

    def create_module(self, spec: Any) -> Optional[ModuleType]:
        return self.original.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self.original.exec_module(module)
        _apply_module_safety(module)


class _SafeFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _SafeLoader(spec.loader)
        return spec


def _cleanup(root: Path) -> None:
    """Remove only our registered temp tree using absolute, non-followed paths.

    fd-based shutil.rmtree emits relative audit paths without their dir_fd on
    Linux, making harmless temp/logs look like production logs. Do not weaken
    the audit guard to compensate: keep cleanup operations absolute instead.
    """
    registered = getattr(sys, _STATE, None)
    if (
        registered is None or root != Path(registered) or not root.is_absolute()
        or not root.name.startswith("lifeos-offline-")
        or root.parent.resolve() != Path(tempfile.gettempdir()).resolve()
        or root.is_symlink() or getattr(root, "is_junction", lambda: False)()
    ):
        raise ValueError("Cleanup requires the registered offline test directory")
    logging.shutdown()
    if not root.exists():
        return
    canonical_root = root.resolve()

    def remove_directory(directory: Path) -> None:
        resolved = directory.resolve()
        if (
            directory.is_symlink()
            or getattr(directory, "is_junction", lambda: False)()
            or (resolved != canonical_root and canonical_root not in resolved.parents)
        ):
            raise ValueError("Cleanup cannot follow a path outside its registered directory")
        with os.scandir(directory) as entries:
            for entry in entries:
                child = Path(entry.path)  # Absolute because scandir received an absolute path.
                if getattr(child, "is_junction", lambda: False)():
                    child.rmdir()  # Remove the Windows junction, never its target.
                elif entry.is_dir(follow_symlinks=False):
                    remove_directory(child)
                else:
                    child.unlink()  # Also removes symlinks without following them.
        directory.rmdir()

    remove_directory(root)


def install_guard() -> Path:
    """Install process-wide isolation once and return the temporary data root."""
    if hasattr(sys, _STATE):
        return Path(getattr(sys, _STATE))
    # pandas uses this cached harmless OS probe on Windows; it may run cmd/ver.
    platform.uname()
    root = Path(tempfile.mkdtemp(prefix="lifeos-offline-"))
    (root / "logs").mkdir()
    setattr(sys, _STATE, str(root))
    atexit.register(_cleanup, root)
    for name in list(os.environ):
        upper = name.upper()
        if upper.endswith("_DB_ID") or any(part in upper for part in (
            "NOTION", "GARMIN", "GARTH", "TELEGRAM", "ANTHROPIC", "OPENROUTER",
            "SHIOAJI", "SCHWAB", "API_KEY", "TOKEN", "PASSWORD", "SECRET",
        )) or upper in {"ONEDRIVE", "ONEDRIVECOMMERCIAL", "ONEDRIVECONSUMER"}:
            os.environ.pop(name, None)
    os.environ.update({
        "NOTION_API_KEY": "offline-fake-notion-key",
        "GARMIN_EMAIL": "offline@example.invalid",
        "GARMIN_PASSWORD": "offline-fake-password",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONUTF8": "1",
    })
    sys.dont_write_bytecode = True
    socket.socketpair = _local_socketpair
    sys.addaudithook(_deny_external_io)
    sys.meta_path.insert(0, _SafeFinder())
    for name in _TARGETS:
        if name in sys.modules:
            _apply_module_safety(sys.modules[name])
    return root


def main(args: Optional[List[str]] = None) -> int:
    """Run pytest only after installing the offline boundary."""
    sys.path.insert(0, str(_ROOT))
    root = install_guard()
    import pytest

    arguments = list(sys.argv[1:] if args is None else args)
    if not arguments:
        arguments = ["tests", "-q"]
    return pytest.main(["-p", "no:cacheprovider", "--basetemp", str(root / "pytest")] + arguments)


if __name__ == "__main__":
    raise SystemExit(main())
