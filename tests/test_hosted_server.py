from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from rich.text import Text
from typer.testing import CliRunner

from matrix_mcp.cli import app
from matrix_mcp.hosted_auth import HostedSettings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "override",
    [
        {"public_base_url": "http://mcp.example.com"},
        {"public_base_url": "https://mcp.example.com/path"},
        {"homeserver": "http://matrix.example.com"},
        {"homeserver": "https://user:password@matrix.example.com"},
        {"homeserver": "https://matrix.example.com?x=y"},
        {"secret_key": "short"},
        {"allowed_client_redirect_uris": []},
        {"allowed_client_redirect_uris": ["https://*.example.com/callback"]},
        {"allowed_client_redirect_uris": ["http://client.example.com/callback"]},
    ],
)
def test_unsafe_hosted_settings_are_rejected(tmp_path: Path, override: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "public_base_url": "https://mcp.example.com",
        "homeserver": "https://matrix.example.com",
        "state_directory": tmp_path,
        "secret_key": "a-stable-test-key-with-at-least-32-characters",
        "allowed_client_redirect_uris": ["https://client.example.com/callback"],
        **override,
    }
    with pytest.raises(ValidationError):
        HostedSettings(**values)


def test_http_cli_validates_missing_settings_before_starting() -> None:
    result = CliRunner().invoke(app, ["serve", "--transport", "http"])
    assert result.exit_code == 2
    assert "public_base_url" in result.output
    assert "secret_key" in result.output
    assert "No such option" not in result.output


def test_serve_help_exposes_http_without_changing_default() -> None:
    result = CliRunner().invoke(app, ["serve", "--help"], color=False)
    output = Text.from_ansi(result.output).plain
    assert result.exit_code == 0
    assert "--transport" in output
    assert "stdio" in output
    assert "http" in output


def test_missing_signing_key_has_no_default(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        HostedSettings.model_validate(
            {
                "public_base_url": "https://mcp.example.com",
                "homeserver": "https://matrix.example.com",
                "state_directory": tmp_path,
                "allowed_client_redirect_uris": ["https://client.example.com/callback"],
            }
        )
