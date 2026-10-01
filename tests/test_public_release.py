"""Public export regressions; all profile numbers are synthetic examples."""
from pathlib import Path
import ast
import socket

import pytest

from config import STOCKS_TW, STOCKS_US
from models import AthleteProfile


def test_public_export_has_no_holdings():
    assert STOCKS_TW == []
    assert STOCKS_US == []


def test_public_export_uses_synthetic_profile():
    profile = AthleteProfile()
    assert profile.weight_kg == 70.0
    assert profile.target_weight_kg == 65.0


def test_dotenv_is_scoped_to_this_checkout():
    source = (Path(__file__).resolve().parents[1] / "config.py").read_text(encoding="utf-8")
    assert 'load_dotenv(Path(__file__).resolve().parent / ".env")' in source


def test_live_network_is_blocked_before_connect():
    with socket.socket() as client:
        with pytest.raises(RuntimeError, match="forbidden"):
            client.connect(("192.0.2.1", 443))


def test_all_dotenv_loads_are_scoped():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "scripts").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "load_dotenv":
                    assert node.args or node.keywords, path.name
