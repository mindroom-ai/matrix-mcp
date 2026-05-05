from __future__ import annotations

from typing import TYPE_CHECKING

from matrix_mcp.config import AuthConfig, MatrixMCPConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_config_round_trips_token_auth(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
        http_headers={"X-Test": "secret"},
    )

    config.save(path)

    assert MatrixMCPConfig.load(path) == config


def test_auth_config_redacts_access_token() -> None:
    config = AuthConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
        http_headers={"X-Test": "secret"},
    )

    dumped = config.model_dump_safe()

    assert dumped["access_token"] == "<configured>"
    assert dumped["http_headers"] == {"X-Test": "<configured>"}
    assert dumped["user_id"] == "@alice:example.com"
