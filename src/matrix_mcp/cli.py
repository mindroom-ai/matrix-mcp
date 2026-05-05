from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path

import typer
from rich.console import Console

from matrix_mcp.auth import (
    SSOCallbackServer,
    SSOProvider,
    build_sso_redirect_url,
    fetch_sso_providers,
    login_with_password,
    login_with_token,
)
from matrix_mcp.config import MatrixMCPConfig, default_config_path
from matrix_mcp.mcp_server import create_mcp_server

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")
console = Console()


@app.command()
def serve() -> None:
    """Run the Matrix MCP server over stdio."""
    create_mcp_server().run()


@app.command()
def config_path() -> None:
    """Print the path to the Matrix MCP config file."""
    console.print(str(default_config_path()))


@auth_app.command("token")
def auth_token(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL, e.g. https://matrix.org"),
    user_id: str = typer.Argument(..., help="Matrix user ID, e.g. @alice:example.com"),
    access_token: str = typer.Argument(..., help="Matrix access token"),
    device_id: str | None = typer.Option(None, help="Optional Matrix device ID"),
    config: Path = typer.Option(default_config_path(), "--config", help="Config file to write"),
) -> None:
    """Store an existing Matrix access token."""
    MatrixMCPConfig(
        homeserver=homeserver.rstrip("/"),
        user_id=user_id,
        device_id=device_id,
        access_token=access_token,
    ).save(config)
    console.print(f"Saved Matrix MCP credentials to {config}")


@auth_app.command("password")
def auth_password(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    user: str = typer.Argument(..., help="Matrix user ID or localpart"),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=False),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    config: Path = typer.Option(default_config_path(), "--config", help="Config file to write"),
) -> None:
    """Login using Matrix password auth and store the resulting access token."""
    result = asyncio.run(
        login_with_password(
            homeserver=homeserver,
            user=user,
            password=password,
            device_name=device_name,
        ),
    )
    result.to_config().save(config)
    console.print(f"Saved Matrix MCP credentials for {result.user_id} to {config}")


@auth_app.command("sso-url")
def auth_sso_url(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    redirect_url: str = typer.Argument(..., help="Callback URL that receives loginToken"),
    idp_id: str | None = typer.Option(None, help="Optional Matrix SSO provider ID"),
    open_browser: bool = typer.Option(False, "--open", help="Open the SSO URL in a browser"),
) -> None:
    """Print a Matrix SSO redirect URL."""
    url = build_sso_redirect_url(homeserver=homeserver, redirect_url=redirect_url, idp_id=idp_id)
    console.print(url)
    if open_browser:
        webbrowser.open(url)


@auth_app.command("providers")
def auth_providers(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
) -> None:
    """List Matrix SSO provider IDs for a homeserver."""
    providers = fetch_sso_providers(homeserver)
    if not providers:
        console.print("No Matrix SSO providers advertised by this homeserver.")
        return
    for provider in providers:
        label = _provider_label(provider)
        console.print(f"{provider.id}\t{label}")


@auth_app.command("sso")
def auth_sso(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    idp_id: str | None = typer.Option(None, help="Optional Matrix SSO provider ID"),
    callback_host: str = typer.Option("127.0.0.1", help="Local callback bind host"),
    callback_port: int = typer.Option(8767, help="Local callback bind port"),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    config: Path = typer.Option(default_config_path(), "--config", help="Config file to write"),
) -> None:
    """Login through Matrix SSO in a browser and save the resulting access token."""
    callback = SSOCallbackServer(host=callback_host, port=callback_port)
    url = build_sso_redirect_url(
        homeserver=homeserver, redirect_url=callback.redirect_url, idp_id=idp_id
    )
    console.print(f"Opening Matrix SSO URL: {url}")
    webbrowser.open(url)
    login_token = callback.wait_for_token()
    result = asyncio.run(
        login_with_token(
            homeserver=homeserver,
            login_token=login_token,
            device_name=device_name,
        ),
    )
    result.to_config().save(config)
    console.print(f"Saved Matrix MCP credentials for {result.user_id} to {config}")


@auth_app.command("login-token")
def auth_login_token(
    homeserver: str = typer.Argument(..., help="Matrix homeserver URL"),
    login_token: str = typer.Argument(..., help="Single-use Matrix m.login.token value"),
    device_name: str = typer.Option("matrix-mcp", help="Matrix device display name"),
    config: Path = typer.Option(default_config_path(), "--config", help="Config file to write"),
) -> None:
    """Exchange a Matrix SSO loginToken for an access token and save it."""
    result = asyncio.run(
        login_with_token(
            homeserver=homeserver,
            login_token=login_token,
            device_name=device_name,
        ),
    )
    result.to_config().save(config)
    console.print(f"Saved Matrix MCP credentials for {result.user_id} to {config}")


@auth_app.command("logout")
def auth_logout(
    config: Path = typer.Option(default_config_path(), "--config", help="Config file to remove"),
) -> None:
    """Remove stored Matrix MCP credentials."""
    if config.exists():
        config.unlink()
        console.print(f"Removed Matrix MCP credentials from {config}")
        return
    console.print(f"No Matrix MCP credentials found at {config}")


def _provider_label(provider: SSOProvider) -> str:
    return provider.name or provider.brand or provider.id


if __name__ == "__main__":
    app()
