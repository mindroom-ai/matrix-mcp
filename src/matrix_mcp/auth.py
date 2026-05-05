from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from nio import AsyncClient, LoginResponse
from pydantic import BaseModel

from matrix_mcp.config import MatrixMCPConfig


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


def extract_login_token(query: str) -> str:
    values = parse_qs(query, keep_blank_values=False)
    token = values.get("loginToken", [None])[0]
    if not token:
        raise ValueError("Matrix SSO callback did not include loginToken")
    return token


class SSOCallbackServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 8767) -> None:
        self.token: str | None = None
        self.error: Exception | None = None

        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
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

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = HTTPServer((host, port), CallbackHandler)
        self.redirect_url = f"http://{host}:{self._server.server_port}/callback"

    def wait_for_token(self) -> str:
        self._server.handle_request()
        self._server.server_close()
        if self.error is not None:
            raise self.error
        if self.token is None:
            raise RuntimeError("Matrix SSO callback did not complete")
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
    if not isinstance(response, LoginResponse):
        raise RuntimeError(f"Matrix password login failed: {response}")
    return LoginResult(
        homeserver=homeserver.rstrip("/"),
        user_id=response.user_id,
        device_id=response.device_id,
        access_token=response.access_token,
    )


async def login_with_token(
    *,
    homeserver: str,
    login_token: str,
    device_name: str = "matrix-mcp",
) -> LoginResult:
    client = AsyncClient(homeserver.rstrip("/"))
    try:
        response = await client.login(token=login_token, device_name=device_name)
    finally:
        await client.close()
    if not isinstance(response, LoginResponse):
        raise RuntimeError(f"Matrix token login failed: {response}")
    return LoginResult(
        homeserver=homeserver.rstrip("/"),
        user_id=response.user_id,
        device_id=response.device_id,
        access_token=response.access_token,
    )
