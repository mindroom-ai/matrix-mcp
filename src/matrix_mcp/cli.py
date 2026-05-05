from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Protocol

import typer

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
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL, e.g. https://matrix.org"),
    user_id: str = typer.Argument(..., help="Matrix user ID, e.g. @alice:example.com"),
    access_token: str = typer.Argument(..., help="Matrix access token"),
    device_id: str | None = typer.Option(None, help="Optional Matrix device ID"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Store an existing Matrix access token."""
    from matrix_mcp.config import MatrixMCPConfig

    config_path = _resolve_config_path(config)
    http_headers = _parse_http_headers(header)
    MatrixMCPConfig(
        homeserver=homeserver.rstrip("/"),
        user_id=user_id,
        device_id=device_id,
        access_token=access_token,
        http_headers=http_headers,
    ).save(config_path)
    typer.echo(f"Saved Matrix MCP credentials to {config_path}")


@auth_app.command("password")
def auth_password(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    user: str = typer.Argument(..., help="Matrix user ID or localpart"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=False),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Login using Matrix password auth and store the resulting access token."""
    from matrix_mcp.auth import login_with_password

    config_path = _resolve_config_path(config)
    http_headers = _parse_http_headers(header)
    result = asyncio.run(
        login_with_password(
            homeserver=homeserver,
            user=user,
            password=password,
            device_name=device_name,
            http_headers=http_headers,
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
) -> None:
    """List Matrix SSO provider IDs for a homeserver."""
    providers = _fetch_sso_providers(homeserver, http_headers=_parse_http_headers(header))
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
    callback_port: int = typer.Option(8767, help="Local callback bind port"),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Login through Matrix SSO in a browser and save the resulting access token."""
    from matrix_mcp.auth import SSOCallbackServer, build_sso_redirect_url, login_with_token

    config_path = _resolve_config_path(config)
    http_headers = _parse_http_headers(header)
    callback = SSOCallbackServer(host=callback_host, port=callback_port)
    url = build_sso_redirect_url(
        homeserver=homeserver, redirect_url=callback.redirect_url, idp_id=idp_id
    )
    typer.echo(f"Opening Matrix SSO URL: {url}")
    webbrowser.open(url)
    login_token = callback.wait_for_token()
    result = asyncio.run(
        login_with_token(
            homeserver=homeserver,
            login_token=login_token,
            device_name=device_name,
            http_headers=http_headers,
        ),
    )
    result.to_config().save(config_path)
    typer.echo(f"Saved Matrix MCP credentials for {result.user_id} to {config_path}")


@auth_app.command("login-token")
def auth_login_token(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    login_token: str = typer.Argument(..., help="Single-use Matrix m.login.token value"),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    header: list[str] | None = typer.Option(None, "--header", "-H", help="Extra HTTP header"),
    config: Path | None = typer.Option(None, "--config", help="Config file to write"),
) -> None:
    """Exchange a Matrix SSO loginToken for an access token and save it."""
    from matrix_mcp.auth import login_with_token

    config_path = _resolve_config_path(config)
    http_headers = _parse_http_headers(header)
    result = asyncio.run(
        login_with_token(
            homeserver=homeserver,
            login_token=login_token,
            device_name=device_name,
            http_headers=http_headers,
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


def _parse_http_headers(header_values: list[str] | None) -> dict[str, str]:
    from matrix_mcp.auth import parse_http_headers

    return parse_http_headers(header_values)


class ProviderLike(Protocol):
    id: str
    name: str | None
    brand: str | None


def _fetch_sso_providers(
    homeserver: str,
    *,
    http_headers: dict[str, str] | None = None,
) -> list[ProviderLike]:
    from matrix_mcp.auth import fetch_sso_providers

    return list(fetch_sso_providers(homeserver, http_headers=http_headers))


def _provider_label(provider: ProviderLike) -> str:
    return provider.name or provider.brand or provider.id


if __name__ == "__main__":
    app()
