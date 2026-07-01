from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import typer
from typer.testing import CliRunner

from matrix_mcp import cli
from matrix_mcp.auth import LoginResult
from matrix_mcp.cli import _with_cloudflare_access_header_command, app
from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.http_headers import HTTPHeaderConfig

if TYPE_CHECKING:
    from pathlib import Path


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


def test_auth_token_stores_extra_http_headers(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "token",
            "https://matrix.example.com",
            "@alice:example.com",
            "test-token",
            "--device-id",
            "TESTDEVICE",
            "--header",
            "X-Access: secret",
            "--header-command",
            "X-Dynamic: print-token",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    saved = MatrixMCPConfig.load(config)
    assert saved.http_headers == {"X-Access": "secret"}
    assert saved.http_header_commands == {"X-Dynamic": "print-token"}


def test_config_path_prints_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "matrix_mcp.config.default_config_path",
        lambda: tmp_path / "config.json",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["config-path"])

    assert result.exit_code == 0
    assert result.output.strip() == str(tmp_path / "config.json")


def test_auth_password_saves_login_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.json"

    async def fake_login_with_password(
        *,
        homeserver: str,
        user: str,
        password: str,
        device_name: str,
        header_config: HTTPHeaderConfig,
    ) -> LoginResult:
        assert homeserver == "https://matrix.example.com"
        assert user == "@alice:example.com"
        assert password == "secret"
        assert device_name == "TESTDEVICE"
        assert header_config == HTTPHeaderConfig(headers={"X-Access": "secret"})
        return LoginResult(
            homeserver=homeserver,
            user_id=user,
            device_id=device_name,
            access_token="test-token",
            http_headers=header_config.headers,
        )

    monkeypatch.setattr("matrix_mcp.auth.login_with_password", fake_login_with_password)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "password",
            "https://matrix.example.com",
            "@alice:example.com",
            "--password",
            "secret",
            "--device-name",
            "TESTDEVICE",
            "--header",
            "X-Access: secret",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    saved = MatrixMCPConfig.load(config)
    assert saved.user_id == "@alice:example.com"
    assert saved.device_id == "TESTDEVICE"
    assert saved.access_token == "test-token"
    assert saved.http_headers == {"X-Access": "secret"}


def test_auth_login_token_saves_login_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"

    async def fake_login_with_token(
        *,
        homeserver: str,
        login_token: str,
        device_name: str,
        header_config: HTTPHeaderConfig,
    ) -> LoginResult:
        assert homeserver == "https://matrix.example.com"
        assert login_token == "login-token"
        assert device_name == "TESTDEVICE"
        assert header_config == HTTPHeaderConfig(commands={"X-Dynamic": "print-token"})
        return LoginResult(
            homeserver=homeserver,
            user_id="@alice:example.com",
            device_id=device_name,
            access_token="test-token",
            http_header_commands=header_config.commands,
        )

    monkeypatch.setattr("matrix_mcp.auth.login_with_token", fake_login_with_token)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "login-token",
            "https://matrix.example.com",
            "login-token",
            "--device-name",
            "TESTDEVICE",
            "--header-command",
            "X-Dynamic: print-token",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    saved = MatrixMCPConfig.load(config)
    assert saved.user_id == "@alice:example.com"
    assert saved.device_id == "TESTDEVICE"
    assert saved.access_token == "test-token"
    assert saved.http_header_commands == {"X-Dynamic": "print-token"}


def test_auth_sso_saves_login_result_after_browser_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    opened_urls: list[str] = []

    class FakeCallback:
        def __init__(self, *, host: str, port: int) -> None:
            assert host == "127.0.0.1"
            assert port == 8767
            self.redirect_url = "http://127.0.0.1:8767/callback"
            self.close_calls = 0
            callbacks.append(self)

        def wait_for_token(self) -> str:
            self.close()
            return "login-token"

        def close(self) -> None:
            self.close_calls += 1

    callbacks: list[FakeCallback] = []

    async def fake_login_with_token(
        *,
        homeserver: str,
        login_token: str,
        device_name: str,
        header_config: HTTPHeaderConfig,
    ) -> LoginResult:
        assert homeserver == "https://matrix.example.com"
        assert login_token == "login-token"
        assert device_name == "TESTDEVICE"
        assert header_config == HTTPHeaderConfig(headers={"X-Access": "secret"})
        return LoginResult(
            homeserver=homeserver,
            user_id="@alice:example.com",
            device_id=device_name,
            access_token="test-token",
            http_headers=header_config.headers,
        )

    monkeypatch.setattr("matrix_mcp.auth.SSOCallbackServer", FakeCallback)
    monkeypatch.setattr("matrix_mcp.auth.login_with_token", fake_login_with_token)
    monkeypatch.setattr("matrix_mcp.cli.webbrowser.open", opened_urls.append)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "sso",
            "https://matrix.example.com",
            "--idp-id",
            "github",
            "--callback-port",
            "8767",
            "--device-name",
            "TESTDEVICE",
            "--header",
            "X-Access: secret",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    assert opened_urls == [
        "https://matrix.example.com/_matrix/client/v3/login/sso/redirect/github?"
        "redirectUrl=http%3A%2F%2F127.0.0.1%3A8767%2Fcallback"
    ]
    assert callbacks[0].close_calls == 2
    saved = MatrixMCPConfig.load(config)
    assert saved.user_id == "@alice:example.com"
    assert saved.access_token == "test-token"


@pytest.mark.parametrize("browser_opened", [True, False])
def test_auth_sso_prints_ssh_hint_when_no_browser_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    browser_opened: bool,
) -> None:
    config = tmp_path / "config.json"

    class FakeCallback:
        def __init__(self, *, host: str, port: int) -> None:
            del host, port
            self.redirect_url = "http://127.0.0.1:8767/callback"

        def wait_for_token(self) -> str:
            return "login-token"

        def close(self) -> None:
            pass

    async def fake_login_with_token(
        *,
        homeserver: str,
        login_token: str,
        device_name: str,
        header_config: HTTPHeaderConfig,
    ) -> LoginResult:
        del login_token, device_name, header_config
        return LoginResult(
            homeserver=homeserver,
            user_id="@alice:example.com",
            device_id="DEVICE",
            access_token="test-token",
        )

    monkeypatch.setattr("matrix_mcp.auth.SSOCallbackServer", FakeCallback)
    monkeypatch.setattr("matrix_mcp.auth.login_with_token", fake_login_with_token)
    monkeypatch.setattr("matrix_mcp.cli.webbrowser.open", lambda _url: browser_opened)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["auth", "sso", "https://matrix.example.com", "--config", str(config)],
    )

    assert result.exit_code == 0
    hint = "ssh -N -L 8767:127.0.0.1:8767"
    assert (hint in result.output) is not browser_opened


def test_auth_sso_cloudflare_access_adds_access_token_header_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    calls: list[str] = []

    class FakeCallback:
        redirect_url = "http://127.0.0.1:8767/callback"

        def __init__(self, *, host: str, port: int) -> None:
            assert host == "127.0.0.1"
            assert port == 0

        def wait_for_token(self) -> str:
            assert calls == ["cloudflare-login", "matrix-sso-browser"]
            return "login-token"

        def close(self) -> None:
            pass

    async def fake_login_with_token(
        *,
        homeserver: str,
        login_token: str,
        device_name: str,
        header_config: HTTPHeaderConfig,
    ) -> LoginResult:
        assert homeserver == "https://matrix.example.com"
        assert login_token == "login-token"
        assert device_name == "matrix-mcp"
        assert header_config == HTTPHeaderConfig(
            commands={"cf-access-token": "cloudflared access token -app=https://matrix.example.com"}
        )
        return LoginResult(
            homeserver=homeserver,
            user_id="@alice:example.com",
            device_id="TESTDEVICE",
            access_token="test-token",
            http_header_commands=header_config.commands,
        )

    monkeypatch.setattr("matrix_mcp.auth.SSOCallbackServer", FakeCallback)
    monkeypatch.setattr("matrix_mcp.auth.login_with_token", fake_login_with_token)
    monkeypatch.setattr(
        "matrix_mcp.cli.webbrowser.open",
        lambda _url: calls.append("matrix-sso-browser"),
    )
    monkeypatch.setattr("matrix_mcp.cli.shutil.which", lambda name: f"/usr/bin/{name}")

    def fake_ensure_cloudflare_access_login(*, homeserver: str) -> None:
        assert homeserver == "https://matrix.example.com"
        calls.append("cloudflare-login")

    monkeypatch.setattr(
        "matrix_mcp.cli._ensure_cloudflare_access_login",
        fake_ensure_cloudflare_access_login,
        raising=False,
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "sso",
            "https://matrix.example.com",
            "--cloudflare-access",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    saved = MatrixMCPConfig.load(config)
    assert saved.http_header_commands == {
        "cf-access-token": "cloudflared access token -app=https://matrix.example.com"
    }


def test_auth_sso_cloudflare_access_requires_cloudflared_before_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_urls: list[str] = []
    callback_starts: list[dict[str, object]] = []

    class FakeCallback:
        redirect_url = "http://127.0.0.1:8767/callback"

        def __init__(self, **kwargs: object) -> None:
            callback_starts.append(kwargs)

        def wait_for_token(self) -> str:
            return "login-token"

        def close(self) -> None:
            pass

    monkeypatch.setattr("matrix_mcp.auth.SSOCallbackServer", FakeCallback)
    monkeypatch.setattr("matrix_mcp.cli.webbrowser.open", opened_urls.append)
    monkeypatch.setattr("matrix_mcp.cli.shutil.which", lambda _name: None)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "sso",
            "https://matrix.example.com",
            "--cloudflare-access",
            "--config",
            str(tmp_path / "config.json"),
        ],
    )

    assert result.exit_code == 2
    assert "cloudflared CLI" in result.output
    assert "brew install cloudflared" in result.output
    assert callback_starts == []
    assert opened_urls == []


def test_cloudflare_access_header_command_reports_missing_cloudflared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("matrix_mcp.cli.shutil.which", lambda _name: None)

    with pytest.raises(typer.BadParameter) as exc_info:
        _with_cloudflare_access_header_command(
            homeserver="https://matrix.example.com",
            header_values=None,
            header_command_values=None,
            enabled=True,
        )

    message = str(exc_info.value)
    assert "--cloudflare-access requires the cloudflared CLI" in message
    assert "brew install cloudflared" in message


def test_cloudflare_access_login_preflight_logs_in_when_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], object, object]] = []
    results = iter(
        [
            SimpleNamespace(returncode=1, stderr=""),
            SimpleNamespace(returncode=0, stderr=""),
            SimpleNamespace(returncode=0, stderr=""),
        ]
    )

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs.get("stdout"), kwargs.get("stderr")))
        return next(results)

    monkeypatch.setattr("matrix_mcp.cli.shutil.which", lambda _name: "/usr/bin/cloudflared")
    monkeypatch.setattr("matrix_mcp.cli.subprocess.run", fake_run)

    cli._ensure_cloudflare_access_login(homeserver="https://matrix.example.com")  # noqa: SLF001

    assert calls == [
        (
            ["/usr/bin/cloudflared", "access", "token", "-app=https://matrix.example.com"],
            subprocess.DEVNULL,
            subprocess.DEVNULL,
        ),
        (["/usr/bin/cloudflared", "access", "login", "https://matrix.example.com"], None, None),
        (
            ["/usr/bin/cloudflared", "access", "token", "-app=https://matrix.example.com"],
            subprocess.DEVNULL,
            subprocess.PIPE,
        ),
    ]


def test_auth_sso_cloudflare_access_rejects_duplicate_access_token_command(
    tmp_path: Path,
) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "sso",
            "https://matrix.example.com",
            "--cloudflare-access",
            "--header-command",
            "cf-access-token: print-token",
            "--config",
            str(tmp_path / "config.json"),
        ],
    )

    assert result.exit_code == 2
    assert "cf-access-token" in result.output


def test_auth_sso_cloudflare_access_rejects_duplicate_access_token_header(
    tmp_path: Path,
) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "sso",
            "https://matrix.example.com",
            "--cloudflare-access",
            "--header",
            "cf-access-token: static-token",
            "--config",
            str(tmp_path / "config.json"),
        ],
    )

    assert result.exit_code == 2
    assert "cf-access-token" in result.output


def test_auth_sso_url_prints_and_opens_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    opened_urls: list[str] = []
    monkeypatch.setattr("matrix_mcp.cli.webbrowser.open", opened_urls.append)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "sso-url",
            "https://matrix.example.com",
            "http://127.0.0.1:8767/callback",
            "--idp-id",
            "github",
            "--open",
        ],
    )

    expected_url = (
        "https://matrix.example.com/_matrix/client/v3/login/sso/redirect/github?"
        "redirectUrl=http%3A%2F%2F127.0.0.1%3A8767%2Fcallback"
    )
    assert result.exit_code == 0
    assert result.output.strip() == expected_url
    assert opened_urls == [expected_url]


def test_auth_providers_lists_sso_provider_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch_sso_providers(
        homeserver: str,
        *,
        header_config: HTTPHeaderConfig | None = None,
    ) -> list[SimpleNamespace]:
        assert homeserver == "https://matrix.example.com"
        assert header_config == HTTPHeaderConfig(
            headers={"X-Access": "secret"},
            commands={"X-Dynamic": "print-token"},
        )
        return [
            SimpleNamespace(id="google", name="Google", brand="google"),
            SimpleNamespace(id="github", name="GitHub", brand="github"),
        ]

    monkeypatch.setattr("matrix_mcp.auth.fetch_sso_providers", fake_fetch_sso_providers)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "auth",
            "providers",
            "https://matrix.example.com",
            "--header",
            "X-Access: secret",
            "--header-command",
            "X-Dynamic: print-token",
        ],
    )

    assert result.exit_code == 0
    assert "google\tGoogle" in result.output
    assert "github\tGitHub" in result.output


def test_auth_providers_reports_empty_provider_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("matrix_mcp.auth.fetch_sso_providers", lambda *_args, **_kwargs: [])
    runner = CliRunner()

    result = runner.invoke(app, ["auth", "providers", "https://matrix.example.com"])

    assert result.exit_code == 0
    assert "No Matrix SSO providers advertised" in result.output
