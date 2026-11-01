from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError
from rich.text import Text
from typer.testing import CliRunner

from matrix_mcp import hosted_server
from matrix_mcp.cli import app
from matrix_mcp.hosted_auth import HostedSettings
from tests.hosted_helpers import CALLBACK

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


def test_static_clients_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MATRIX_MCP_HOSTED_STATIC_CLIENTS",
        '{"desktop-client": ["https://client.example.com/callback", "http://127.0.0.1:8765/"]}',
    )
    settings = HostedSettings(
        public_base_url="https://mcp.example.com",
        homeserver="https://matrix.example.com",
        state_directory=tmp_path,
        secret_key="a-stable-test-key-with-at-least-32-characters",
    )
    assert settings.static_clients == {"desktop-client": [CALLBACK, "http://127.0.0.1:8765/"]}


@pytest.mark.parametrize(
    "clients",
    [
        {"": [CALLBACK]},
        {"has spaces": [CALLBACK]},
        {"https://client.example.com": [CALLBACK]},
        {"../client": [CALLBACK]},
        {"client": []},
        {"client": ["http://client.example.com/callback"]},
        {"client": ["https://*.example.com/callback"]},
        {"client": [f"{CALLBACK}?query=value"]},
        {"client": [f"{CALLBACK}#fragment"]},
        {"client": ["https://user:password@client.example.com/callback"]},
    ],
)
def test_invalid_static_clients_are_rejected(tmp_path: Path, clients: dict[str, list[str]]) -> None:
    with pytest.raises(ValidationError) as error:
        HostedSettings(
            public_base_url="https://mcp.example.com",
            homeserver="https://matrix.example.com",
            state_directory=tmp_path,
            secret_key="a-stable-test-key-with-at-least-32-characters",
            allowed_client_redirect_uris=[CALLBACK],
            static_clients=clients,
        )
    assert error.value.errors()[0]["type"] == "value_error"


@pytest.mark.parametrize(
    "entry", ["missing-separator", "bad id=https://client.example.com/callback"]
)
def test_cli_rejects_invalid_static_clients(entry: str) -> None:
    result = CliRunner().invoke(app, ["serve", "--transport", "http", "--static-client", entry])
    assert result.exit_code == 2
    assert "No such option" not in result.output


def test_cli_static_clients_override_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "MATRIX_MCP_HOSTED_SECRET_KEY", "a-stable-test-key-with-at-least-32-characters"
    )
    monkeypatch.setenv(
        "MATRIX_MCP_HOSTED_STATIC_CLIENTS", '{"env-client": ["https://env.example.com/"]}'
    )
    received: list[HostedSettings] = []
    create = hosted_server.create_hosted_server

    def capture(settings: HostedSettings) -> FastMCP:
        received.append(settings)
        return create(settings)

    monkeypatch.setattr(hosted_server, "create_hosted_server", capture)
    monkeypatch.setattr(FastMCP, "run", lambda *_args, **_kwargs: None)
    result = CliRunner().invoke(
        app,
        [
            "serve",
            "--transport",
            "http",
            "--public-base-url",
            "https://mcp.example.com",
            "--homeserver",
            "https://matrix.example.com",
            "--state-directory",
            str(tmp_path),
            "--static-client",
            f"desktop-client={CALLBACK}",
            "--static-client",
            "desktop-client=http://127.0.0.1:8765/callback",
            "--static-client",
            "other-client=https://other.example.com/callback",
        ],
    )
    assert result.exit_code == 0, result.output
    assert received[0].static_clients == {
        "desktop-client": [CALLBACK, "http://127.0.0.1:8765/callback"],
        "other-client": ["https://other.example.com/callback"],
    }
