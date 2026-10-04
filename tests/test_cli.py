from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import typer
from filelock import FileLock
from typer.testing import CliRunner

from matrix_mcp import cli
from matrix_mcp.auth import LoginResult
from matrix_mcp.cli import _with_cloudflare_access_header_command, app
from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.e2ee import E2EEStatus, E2EEUnavailableError, e2ee_lock_path, e2ee_store_path
from matrix_mcp.http_headers import HTTPHeaderConfig


class FakeE2EE:
    instances: ClassVar[list[FakeE2EE]] = []
    failure: ClassVar[str | None] = None

    def __init__(self, config: MatrixMCPConfig, **kwargs: Any) -> None:
        del kwargs
        self.config = config
        self.imported: tuple[Path, str] | None = None
        FakeE2EE.instances.append(self)

    async def setup(self) -> E2EEStatus:
        if FakeE2EE.failure is not None:
            raise E2EEUnavailableError(FakeE2EE.failure)
        assert self.config.device_id is not None
        return E2EEStatus(
            device_id=self.config.device_id,
            fingerprint="FINGERPRINT",
            store_path=Path("store"),
        )

    async def import_keys(self, path: Path, passphrase: str) -> None:
        if passphrase != "right":
            msg = "Could not import room keys: wrong passphrase or invalid key export file"
            raise ValueError(msg)
        self.imported = (path, passphrase)


@pytest.fixture(autouse=True)
def fake_e2ee(monkeypatch: pytest.MonkeyPatch) -> type[FakeE2EE]:
    FakeE2EE.instances.clear()
    FakeE2EE.failure = None
    monkeypatch.setattr("matrix_mcp.e2ee.MatrixE2EE", FakeE2EE)
    return FakeE2EE


def write_config(path: Path, *, device_id: str | None = "TESTDEVICE") -> MatrixMCPConfig:
    config = MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id=device_id,
        access_token="test-token",
    )
    config.save(path)
    return config


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


def test_login_sets_up_encryption_for_the_new_device(tmp_path: Path) -> None:
    config = tmp_path / "config.json"

    result = CliRunner().invoke(
        app,
        [
            "auth",
            "token",
            "https://matrix.example.com",
            "@alice:example.com",
            "test-token",
            "--device-id",
            "TESTDEVICE",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    assert "End-to-end encryption ready for device TESTDEVICE" in result.output
    assert "FINGERPRINT" in result.output
    assert FakeE2EE.instances[0].config.device_id == "TESTDEVICE"


def test_login_keeps_credentials_when_encryption_setup_fails(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    FakeE2EE.failure = "Matrix rejected this device's encryption keys"

    result = CliRunner().invoke(
        app,
        [
            "auth",
            "token",
            "https://matrix.example.com",
            "@alice:example.com",
            "test-token",
            "--device-id",
            "TESTDEVICE",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    assert MatrixMCPConfig.load(config).access_token == "test-token"
    assert "Matrix rejected this device's encryption keys" in result.output
    assert "matrix-mcp e2ee setup" in result.output


def test_login_without_device_id_skips_encryption_setup(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "auth",
            "token",
            "https://matrix.example.com",
            "@alice:example.com",
            "test-token",
            "--config",
            str(tmp_path / "config.json"),
        ],
    )

    assert result.exit_code == 0
    assert FakeE2EE.instances == []
    assert "device ID" in result.output


def test_e2ee_setup_reports_device_and_failure(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    write_config(config)

    ready = CliRunner().invoke(app, ["e2ee", "setup", "--config", str(config)])
    FakeE2EE.failure = "store is in use"
    failed = CliRunner().invoke(app, ["e2ee", "setup", "--config", str(config)])

    assert ready.exit_code == 0
    assert "TESTDEVICE" in ready.output
    assert "FINGERPRINT" in ready.output
    assert failed.exit_code == 1
    assert "store is in use" in failed.output


@pytest.mark.parametrize(("passphrase", "exit_code"), [("right", 0), ("wrong", 1)])
def test_e2ee_import_keys_uses_prompted_passphrase(
    tmp_path: Path, passphrase: str, exit_code: int
) -> None:
    config = tmp_path / "config.json"
    write_config(config)
    export = tmp_path / "element-keys.txt"
    export.write_text("export", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["e2ee", "import-keys", str(export), "--config", str(config)],
        input=f"{passphrase}\n",
    )

    assert result.exit_code == exit_code
    if exit_code == 0:
        assert FakeE2EE.instances[0].imported == (export, "right")
        assert "Imported room keys" in result.output
    else:
        assert "wrong passphrase" in result.output


def test_auth_logout_removes_the_device_encryption_store(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    store = e2ee_store_path(write_config(config))
    store.mkdir()
    (store / "keys.db").write_bytes(b"keys")

    result = CliRunner().invoke(app, ["auth", "logout", "--config", str(config)])

    assert result.exit_code == 0
    assert not config.exists()
    assert not store.exists()
    assert "Removed end-to-end encryption keys" in result.output
    assert "still registered" in result.output


def test_auth_logout_waits_for_the_encryption_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("matrix_mcp.cli._E2EE_LOCK_TIMEOUT_SECONDS", 0.05)
    config = tmp_path / "config.json"
    store = e2ee_store_path(write_config(config))
    store.mkdir()

    with FileLock(e2ee_lock_path(store)):
        result = CliRunner().invoke(app, ["auth", "logout", "--config", str(config)])

    assert result.exit_code == 1
    assert "in use" in result.output
    assert config.exists()
    assert store.exists()


@pytest.mark.parametrize("command", ["setup", "import-keys"])
def test_e2ee_commands_explain_missing_credentials(tmp_path: Path, command: str) -> None:
    export = tmp_path / "keys.txt"
    export.write_text("export", encoding="utf-8")
    arguments = ["e2ee", command, "--config", str(tmp_path / "missing.json")]
    if command == "import-keys":
        arguments.insert(2, str(export))

    result = CliRunner().invoke(app, arguments, input="pass\n")

    assert result.exit_code == 1
    assert "Cannot read Matrix MCP credentials" in result.output
