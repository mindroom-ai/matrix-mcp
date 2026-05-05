from __future__ import annotations

from typing import TYPE_CHECKING

from matrix_mcp.config import AuthConfig, MatrixMCPConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_config_round_trips_token_auth(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
        http_headers={"X-Test": "secret"},
        http_header_commands={"X-Dynamic": "print-token"},
    )

    config.save(path)

    assert MatrixMCPConfig.load(path) == config


def test_default_config_loads_existing_home_dot_config_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform_config_dir = tmp_path / "Library" / "Application Support" / "matrix-mcp"
    fallback_config = tmp_path / ".config" / "matrix-mcp" / "config.json"
    fallback_config.parent.mkdir(parents=True)
    fallback_config.write_text(
        MatrixMCPConfig(
            homeserver="https://matrix.example.com",
            user_id="@alice:example.com",
            device_id="TESTDEVICE",
            access_token="test-token",
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def fake_user_config_dir(_appname: str, *, appauthor: bool) -> str:
        assert appauthor is False
        return str(platform_config_dir)

    monkeypatch.setattr(
        "matrix_mcp.config.user_config_dir",
        fake_user_config_dir,
    )

    config = MatrixMCPConfig.load()

    assert config.user_id == "@alice:example.com"


def test_auth_config_redacts_access_token() -> None:
    config = AuthConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="CLAUDECODE",
        access_token="test-token",
        http_headers={"X-Test": "secret"},
        http_header_commands={"X-Dynamic": "print-token"},
    )

    dumped = config.model_dump_safe()

    assert dumped["access_token"] == "<configured>"
    assert dumped["http_headers"] == {"X-Test": "<configured>"}
    assert dumped["http_header_commands"] == {"X-Dynamic": "<configured>"}
    assert dumped["user_id"] == "@alice:example.com"
