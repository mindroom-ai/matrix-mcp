from __future__ import annotations

from pathlib import Path

from matrix_mcp.config import AuthConfig, MatrixMCPConfig


def test_config_round_trips_token_auth(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
    )

    config.save(path)

    assert MatrixMCPConfig.load(path) == config


def test_auth_config_redacts_access_token() -> None:
    config = AuthConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
    )

    dumped = config.model_dump_safe()

    assert dumped["access_token"] == "<configured>"
    assert dumped["user_id"] == "@alice:example.com"
