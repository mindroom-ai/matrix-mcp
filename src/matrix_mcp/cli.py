from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import typer

if TYPE_CHECKING:
    from matrix_mcp.http_headers import HTTPHeaderConfig

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")


@app.command()
def serve() -> None:
    """Run the Matrix MCP server over stdio."""
    from matrix_mcp.mcp_server import create_mcp_server

    create_mcp_server().run()


@app.command()
def config_path() -> None:
    """Print the path to the Matrix MCP config file."""
    typer.echo(str(_resolve_config_path(None)))


@auth_app.command("token")
def auth_token(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL, e.g. https://mindroom.chat"),
    user_id: str = typer.Argument(..., help="Matrix user ID, e.g. @alice:mindroom.chat"),
    access_token: str = typer.Argument(..., help="Matrix access token"),
    device_id: str | None = typer.Option(None, help="Optional Matrix device ID"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    header_command: list[str] | None = typer.Option(
        None,
        "--header-command",
        help="Command that prints an extra HTTP header value, formatted as 'Name: command'",
    ),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Store an existing Matrix access token."""
    from matrix_mcp.config import MatrixMCPConfig

    config_path = _resolve_config_path(config)
    header_config = _http_header_config(header, header_command)
    MatrixMCPConfig(
        homeserver=homeserver.rstrip("/"),
        user_id=user_id,
        device_id=device_id,
        access_token=access_token,
        http_headers=header_config.headers,
        http_header_commands=header_config.commands,
    ).save(config_path)
    typer.echo(f"Saved Matrix MCP credentials to {config_path}")


@auth_app.command("password")
def auth_password(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    user: str = typer.Argument(..., help="Matrix user ID or localpart"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=False),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    header_command: list[str] | None = typer.Option(
        None,
        "--header-command",
        help="Command that prints an extra HTTP header value, formatted as 'Name: command'",
    ),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Login using Matrix password auth and store the resulting access token."""
    from matrix_mcp.auth import login_with_password

    config_path = _resolve_config_path(config)
    header_config = _http_header_config(header, header_command)
    result = asyncio.run(
        login_with_password(
            homeserver=homeserver,
            user=user,
            password=password,
            device_name=device_name,
            header_config=header_config,
        ),
    )
    result.to_config().save(config_path)
    typer.echo(f"Saved Matrix MCP credentials for {result.user_id} to {config_path}")


@auth_app.command("sso-url")
def auth_sso_url(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    redirect_url: str = typer.Argument(..., help="Callback URL that receives loginToken"),
    idp_id: str | None = typer.Option(None, help="Optional Matrix SSO provider ID"),
    open_browser: bool = typer.Option(False, "--open", help="Open the SSO URL in a browser"),
) -> None:
    """Print a Matrix SSO redirect URL."""
    from matrix_mcp.auth import build_sso_redirect_url

    url = build_sso_redirect_url(homeserver=homeserver, redirect_url=redirect_url, idp_id=idp_id)
    typer.echo(url)
    if open_browser:
        webbrowser.open(url)


@auth_app.command("providers")
def auth_providers(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    header_command: list[str] | None = typer.Option(
        None,
        "--header-command",
        help="Command that prints an extra HTTP header value, formatted as 'Name: command'",
    ),
) -> None:
    """List Matrix SSO provider IDs for a homeserver."""
    providers = _fetch_sso_providers(
        homeserver,
        header_config=_http_header_config(header, header_command),
    )
    if not providers:
        typer.echo("No Matrix SSO providers advertised by this homeserver.")
        return
    for provider in providers:
        label = _provider_label(provider)
        typer.echo(f"{provider.id}\t{label}")


@auth_app.command("sso")
def auth_sso(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    idp_id: str | None = typer.Option(None, help="Optional Matrix SSO provider ID"),
    callback_host: str = typer.Option("127.0.0.1", help="Local callback bind host"),
    callback_port: int = typer.Option(0, help="Local callback bind port; 0 chooses a free port"),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    header_command: list[str] | None = typer.Option(
        None,
        "--header-command",
        help="Command that prints an extra HTTP header value, formatted as 'Name: command'",
    ),
    cloudflare_access: bool = typer.Option(
        False,
        "--cloudflare-access",
        help="Use cloudflared on PATH to provide a Cloudflare Access token header",
    ),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Login through Matrix SSO in a browser and save the resulting access token."""
    from matrix_mcp.auth import SSOCallbackServer, build_sso_redirect_url, login_with_token

    config_path = _resolve_config_path(config)
    header_command = _with_cloudflare_access_header_command(
        homeserver=homeserver,
        header_values=header,
        header_command_values=header_command,
        enabled=cloudflare_access,
    )
    if cloudflare_access:
        _ensure_cloudflare_access_login(homeserver=homeserver)
    header_config = _http_header_config(
        header,
        header_command,
    )
    callback = SSOCallbackServer(host=callback_host, port=callback_port)
    try:
        url = build_sso_redirect_url(
            homeserver=homeserver, redirect_url=callback.redirect_url, idp_id=idp_id
        )
        typer.echo(f"Opening Matrix SSO URL: {url}")
        if not webbrowser.open(url):
            typer.echo(
                "No browser opened on this machine. If you are connected over SSH, "
                "re-run with --callback-port PORT, forward the port from the machine "
                "with your browser (ssh -L PORT:127.0.0.1:PORT remote-host), and open "
                "the URL above there. See https://matrix-mcp.mindroom.chat/getting-started/."
            )
        login_token = callback.wait_for_token()
        result = asyncio.run(
            login_with_token(
                homeserver=homeserver,
                login_token=login_token,
                device_name=device_name,
                header_config=header_config,
            ),
        )
    finally:
        callback.close()
    result.to_config().save(config_path)
    typer.echo(f"Saved Matrix MCP credentials for {result.user_id} to {config_path}")


@auth_app.command("login-token")
def auth_login_token(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    login_token: str = typer.Argument(..., help="Single-use Matrix m.login.token value"),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    header_command: list[str] | None = typer.Option(
        None,
        "--header-command",
        help="Command that prints an extra HTTP header value, formatted as 'Name: command'",
    ),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Exchange a Matrix SSO loginToken for an access token and save it."""
    from matrix_mcp.auth import login_with_token

    config_path = _resolve_config_path(config)
    header_config = _http_header_config(header, header_command)
    result = asyncio.run(
        login_with_token(
            homeserver=homeserver,
            login_token=login_token,
            device_name=device_name,
            header_config=header_config,
        ),
    )
    result.to_config().save(config_path)
    typer.echo(f"Saved Matrix MCP credentials for {result.user_id} to {config_path}")


@auth_app.command("logout")
def auth_logout(
    config: Path | None = typer.Option(None, "--config", help="Config file to remove"),
) -> None:
    """Remove stored Matrix MCP credentials."""
    config_path = _resolve_config_path(config)
    if config_path.exists():
        config_path.unlink()
        typer.echo(f"Removed Matrix MCP credentials from {config_path}")
        return
    typer.echo(f"No Matrix MCP credentials found at {config_path}")


def _resolve_config_path(config: Path | None) -> Path:
    if config is not None:
        return config
    from matrix_mcp.config import default_config_path

    return default_config_path()


def _http_header_config(
    header_values: list[str] | None,
    header_command_values: list[str] | None,
) -> HTTPHeaderConfig:
    from matrix_mcp.http_headers import (
        HTTPHeaderConfig,
        parse_http_header_commands,
        parse_http_headers,
    )

    return HTTPHeaderConfig(
        headers=parse_http_headers(header_values),
        commands=parse_http_header_commands(header_command_values),
    )


def _with_cloudflare_access_header_command(
    *,
    homeserver: str,
    header_values: list[str] | None,
    header_command_values: list[str] | None,
    enabled: bool,
) -> list[str] | None:
    if not enabled:
        return header_command_values

    commands = list(header_command_values or [])
    header_name = "cf-access-token"
    configured_header_names = [
        raw.partition(":")[0].strip().lower() for raw in [*(header_values or []), *commands]
    ]
    if header_name in configured_header_names:
        msg = "--cloudflare-access cannot be combined with a cf-access-token header"
        raise typer.BadParameter(msg)
    if shutil.which("cloudflared") is None:
        from matrix_mcp.http_headers import cloudflared_missing_message

        raise typer.BadParameter(cloudflared_missing_message("--cloudflare-access"))

    command = shlex.join(_cloudflare_access_token_args(homeserver.rstrip("/")))
    commands.append(f"{header_name}: {command}")
    return commands


def _ensure_cloudflare_access_login(*, homeserver: str) -> None:
    cloudflared = _cloudflared_executable()
    app_url = homeserver.rstrip("/")
    token_args = _cloudflare_access_token_args(app_url, executable=cloudflared)
    if _cloudflare_access_token_available(token_args):
        return

    typer.echo("Opening Cloudflare Access login with cloudflared...", err=True)
    try:
        login_completed = subprocess.run(  # noqa: S603
            [cloudflared, "access", "login", app_url],
            check=False,
        )
    except OSError as exc:
        msg = f"Failed to run cloudflared access login: {exc}"
        raise typer.BadParameter(msg) from exc
    if login_completed.returncode != 0:
        msg = f"cloudflared access login failed with exit {login_completed.returncode}"
        raise typer.BadParameter(msg)

    try:
        token_completed = subprocess.run(  # noqa: S603
            token_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        msg = f"Failed to run cloudflared access token: {exc}"
        raise typer.BadParameter(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = "cloudflared access token timed out after Cloudflare Access login"
        raise typer.BadParameter(msg) from exc

    if token_completed.returncode != 0:
        detail = token_completed.stderr.strip() or f"exit {token_completed.returncode}"
        msg = f"cloudflared access token failed after Cloudflare Access login: {detail}"
        raise typer.BadParameter(msg)


def _cloudflare_access_token_available(token_args: list[str]) -> bool:
    try:
        completed = subprocess.run(  # noqa: S603
            token_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _cloudflare_access_token_args(app_url: str, *, executable: str = "cloudflared") -> list[str]:
    return [executable, "access", "token", f"-app={app_url}"]


def _cloudflared_executable() -> str:
    executable = shutil.which("cloudflared")
    if executable is not None:
        return executable

    from matrix_mcp.http_headers import cloudflared_missing_message

    raise typer.BadParameter(cloudflared_missing_message("--cloudflare-access"))


class ProviderLike(Protocol):
    id: str
    name: str | None
    brand: str | None


def _fetch_sso_providers(
    homeserver: str,
    *,
    header_config: HTTPHeaderConfig | None = None,
) -> list[ProviderLike]:
    from matrix_mcp.auth import fetch_sso_providers

    return list(fetch_sso_providers(homeserver, header_config=header_config))


def _provider_label(provider: ProviderLike) -> str:
    return provider.name or provider.brand or provider.id


if __name__ == "__main__":
    app()
