from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from nio import AsyncClient, LoginResponse
from pydantic import BaseModel, ConfigDict

from matrix_mcp.config import MatrixMCPConfig


class SSOProvider(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str | None = None
    brand: str | None = None


class LoginResult(BaseModel):
    homeserver: str
    user_id: str
    device_id: str | None
    access_token: str

    def to_config(self) -> MatrixMCPConfig:
        return MatrixMCPConfig(
            homeserver=self.homeserver,
            user_id=self.user_id,
            device_id=self.device_id,
            access_token=self.access_token,
        )


def build_sso_redirect_url(*, homeserver: str, redirect_url: str, idp_id: str | None = None) -> str:
    base = homeserver.rstrip("/")
    provider = f"/{quote(idp_id, safe='')}" if idp_id else ""
    query = urlencode({"redirectUrl": redirect_url})
    return f"{base}/_matrix/client/v3/login/sso/redirect{provider}?{query}"


def parse_sso_providers(login_response: dict[str, Any]) -> list[SSOProvider]:
    providers: list[SSOProvider] = []
    for flow in login_response.get("flows", []):
        if not isinstance(flow, dict) or flow.get("type") != "m.login.sso":
            continue
        providers.extend(
            SSOProvider.model_validate(provider)
            for provider in flow.get("identity_providers", [])
            if isinstance(provider, dict) and isinstance(provider.get("id"), str)
        )
    return providers


def fetch_sso_providers(homeserver: str) -> list[SSOProvider]:
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/login"
    response = httpx.get(url, timeout=10)
    response.raise_for_status()
    return parse_sso_providers(response.json())


def extract_login_token(query: str) -> str:
    values = parse_qs(query, keep_blank_values=False)
    token = values.get("loginToken", [None])[0]
    if not token:
        msg = "Matrix SSO callback did not include loginToken"
        raise ValueError(msg)
    return token


class SSOCallbackServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 8767) -> None:
        self.token: str | None = None
        self.error: Exception | None = None

        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    owner.token = extract_login_token(urlsplit(self.path).query)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"Matrix MCP login complete. You can close this tab.")
                except Exception as exc:  # noqa: BLE001
                    owner.error = exc
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Matrix MCP login failed. Return to the terminal.")

            def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
                del format, _args

        self._server = HTTPServer((host, port), CallbackHandler)
        self.redirect_url = f"http://{host}:{self._server.server_port}/callback"

    def wait_for_token(self) -> str:
        self._server.handle_request()
        self._server.server_close()
        if self.error is not None:
            raise self.error
        if self.token is None:
            msg = "Matrix SSO callback did not complete"
            raise RuntimeError(msg)
        return self.token


async def login_with_password(
    *,
    homeserver: str,
    user: str,
    password: str,
    device_name: str = "matrix-mcp",
) -> LoginResult:
    client = AsyncClient(homeserver.rstrip("/"), user)
    try:
        response = await client.login(password=password, device_name=device_name)
    finally:
        await client.close()
    if isinstance(response, LoginResponse):
        return LoginResult(
            homeserver=homeserver.rstrip("/"),
            user_id=response.user_id,
            device_id=response.device_id,
            access_token=response.access_token,
        )
    msg = f"Matrix password login failed: {response}"
    raise RuntimeError(msg)


async def login_with_token(
    *,
    homeserver: str,
    login_token: str,
    device_name: str = "matrix-mcp",
    http_client: httpx.AsyncClient | None = None,
) -> LoginResult:
    normalized_homeserver = homeserver.rstrip("/")
    if http_client is not None:
        return await _login_with_token_http(
            http_client=http_client,
            homeserver=normalized_homeserver,
            login_token=login_token,
            device_name=device_name,
        )

    async with httpx.AsyncClient(timeout=10) as client:
        return await _login_with_token_http(
            http_client=client,
            homeserver=normalized_homeserver,
            login_token=login_token,
            device_name=device_name,
        )


async def _login_with_token_http(
    *,
    http_client: httpx.AsyncClient,
    homeserver: str,
    login_token: str,
    device_name: str,
) -> LoginResult:
    response = await http_client.post(
        f"{homeserver}/_matrix/client/v3/login",
        json={
            "type": "m.login.token",
            "token": login_token,
            "initial_device_display_name": device_name,
        },
    )
    response.raise_for_status()
    data = _response_json_object(response, context="Matrix token login response")

    access_token = _required_string(data, "access_token", context="Matrix token login response")
    device_id = data.get("device_id")
    user_id = data.get("user_id")
    if not isinstance(user_id, str):
        user_id = await _fetch_user_id(
            http_client=http_client,
            homeserver=homeserver,
            access_token=access_token,
        )

    return LoginResult(
        homeserver=homeserver,
        user_id=user_id,
        device_id=device_id if isinstance(device_id, str) else None,
        access_token=access_token,
    )


async def _fetch_user_id(
    *,
    http_client: httpx.AsyncClient,
    homeserver: str,
    access_token: str,
) -> str:
    response = await http_client.get(
        f"{homeserver}/_matrix/client/v3/account/whoami",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    response.raise_for_status()
    data = _response_json_object(response, context="Matrix whoami response")
    return _required_string(data, "user_id", context="Matrix whoami response")


def _response_json_object(response: httpx.Response, *, context: str) -> dict[str, Any]:
    data = response.json()
    if isinstance(data, dict):
        return data
    msg = f"{context} was not a JSON object"
    raise RuntimeError(msg)


def _required_string(data: dict[str, Any], key: str, *, context: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    msg = f"{context} did not include required string field {key!r}"
    raise RuntimeError(msg)
