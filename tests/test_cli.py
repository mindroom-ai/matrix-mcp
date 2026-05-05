from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from matrix_mcp.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_auth_logout_removes_stored_credentials(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"homeserver": "https://matrix.example.com"}\n', encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["auth", "logout", "--config", str(config)])

    assert result.exit_code == 0
    assert not config.exists()
    assert "Removed Matrix MCP credentials" in result.output


def test_auth_logout_is_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "missing.json"
    runner = CliRunner()

    result = runner.invoke(app, ["auth", "logout", "--config", str(config)])

    assert result.exit_code == 0
    assert "No Matrix MCP credentials found" in result.output


def test_auth_providers_lists_sso_provider_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch_sso_providers(homeserver: str) -> list[SimpleNamespace]:
        assert homeserver == "https://matrix.example.com"
        return [
            SimpleNamespace(id="google", name="Google", brand="google"),
            SimpleNamespace(id="github", name="GitHub", brand="github"),
        ]

    monkeypatch.setattr("matrix_mcp.cli._fetch_sso_providers", fake_fetch_sso_providers)
    runner = CliRunner()

    result = runner.invoke(app, ["auth", "providers", "https://matrix.example.com"])

    assert result.exit_code == 0
    assert "google\tGoogle" in result.output
    assert "github\tGitHub" in result.output
