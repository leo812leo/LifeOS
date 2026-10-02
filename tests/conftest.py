"""Install offline isolation BEFORE pytest imports any test/application module."""

import sys
from pathlib import Path

# 確保專案根目錄在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.test_offline import install_guard

_OFFLINE_ROOT = install_guard()


def pytest_configure(config) -> None:
    """Keep pytest temporary data out of the production working directory."""
    if config.option.basetemp is None:
        config.option.basetemp = str(_OFFLINE_ROOT / "pytest")
