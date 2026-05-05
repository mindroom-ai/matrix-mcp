from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from matrix_mcp.cli import app


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
