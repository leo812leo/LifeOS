"""Inactive connector examples must be consistent and contain no credentials."""

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "notion": "https://mcp.notion.com/mcp",
    "heptabase": "https://api.heptabase.com/mcp",
}


def test_claude_example_has_only_official_http_endpoints() -> None:
    path = ROOT / "examples" / "connectors" / "claude.mcp.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert set(config) == {"mcpServers"}
    assert set(config["mcpServers"]) == set(EXPECTED)
    for name, url in EXPECTED.items():
        assert config["mcpServers"][name] == {"type": "http", "url": url}


def test_codex_example_has_only_official_http_endpoints() -> None:
    path = ROOT / "examples" / "connectors" / "codex.config.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    assert set(config) == {"mcp_servers"}
    assert set(config["mcp_servers"]) == set(EXPECTED)
    for name, url in EXPECTED.items():
        assert config["mcp_servers"][name] == {"url": url}
