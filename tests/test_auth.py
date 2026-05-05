from __future__ import annotations

import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

import httpx
import pytest
from nio import AsyncClientConfig, LoginResponse

from matrix_mcp.auth import (
    SSOCallbackServer,
    SSOProvider,
    build_sso_redirect_url,
    extract_login_token,
    fetch_sso_providers,
    login_with_password,
    login_with_token,
    parse_sso_providers,
)
from matrix_mcp.http_headers import (
    HTTPHeaderConfig,
    parse_http_header_commands,
    parse_http_headers,
    resolve_http_headers,
)


def test_build_sso_redirect_url_includes_redirect_url() -> None:
    url = build_sso_redirect_url(
        homeserver="https://matrix.example.com",
        redirect_url="http://127.0.0.1:8767/callback",
    )

    assert url == (
        "https://matrix.example.com/_matrix/client/v3/login/sso/redirect?"
        "redirectUrl=http%3A%2F%2F127.0.0.1%3A8767%2Fcallback"
    )


def test_sso_callback_server_default_uses_free_port() -> None:
    with socket.socket() as busy_socket:
        busy_socket.bind(("127.0.0.1", 8767))
        busy_socket.listen()

        callback = SSOCallbackServer()
        try:
            assert callback.redirect_url.startswith("http://127.0.0.1:")
            assert callback.redirect_url != "http://127.0.0.1:8767/callback"
        finally:
            callback.close()


def test_sso_callback_server_accepts_browser_callback() -> None:
    callback = SSOCallbackServer()

    with ThreadPoolExecutor(max_workers=1) as executor:
        token_future = executor.submit(callback.wait_for_token)
        response = httpx.get(f"{callback.redirect_url}?loginToken=abc123")

    assert response.status_code == 200
    assert token_future.result(timeout=1) == "abc123"


def test_sso_callback_server_reports_browser_callback_errors() -> None:
    callback = SSOCallbackServer()

    with ThreadPoolExecutor(max_workers=1) as executor:
        token_future = executor.submit(callback.wait_for_token)
        response = httpx.get(callback.redirect_url)

    assert response.status_code == 400
    with pytest.raises(ValueError, match="loginToken"):
        token_future.result(timeout=1)


def test_extract_login_token_from_callback_query() -> None:
    assert extract_login_token("loginToken=abc123&state=ignored") == "abc123"


def test_extract_login_token_requires_token() -> None:
    with pytest.raises(ValueError, match="loginToken"):
        extract_login_token("state=ignored")


def test_parse_sso_providers_reads_matrix_login_flows() -> None:
    providers = parse_sso_providers(
        {
            "flows": [
                {"type": "m.login.password"},
                {
                    "type": "m.login.sso",
                    "identity_providers": [
                        {"id": "google", "name": "Google", "brand": "google"},
                        {"id": "github", "name": "GitHub", "brand": "github"},
                    ],
                },
            ],
        }
    )

    assert providers == [
        SSOProvider(id="google", name="Google", brand="google"),
        SSOProvider(id="github", name="GitHub", brand="github"),
    ]


def test_fetch_sso_providers_reads_matrix_login_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, str] | None, int]] = []

    def fake_get(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int,
    ) -> httpx.Response:
        requests.append((url, headers, timeout))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "flows": [
                    {
                        "type": "m.login.sso",
                        "identity_providers": [{"id": "github", "name": "GitHub"}],
                    }
                ]
            },
        )

    monkeypatch.setattr("matrix_mcp.auth.httpx.get", fake_get)

    providers = fetch_sso_providers(
        "https://matrix.example.com/",
        header_config=HTTPHeaderConfig(headers={"X-Access": "secret"}),
    )

    assert providers == [SSOProvider(id="github", name="GitHub", brand=None)]
    assert requests == [
        (
            "https://matrix.example.com/_matrix/client/v3/login",
            {"X-Access": "secret"},
            10,
        )
    ]


def test_parse_http_headers_parses_repeated_header_values() -> None:
    assert parse_http_headers(["X-First: one", "X-Second: two: still two"]) == {
        "X-First": "one",
        "X-Second": "two: still two",
    }


def test_parse_http_headers_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="expected 'Name: value'"):
        parse_http_headers(["not-a-header"])


def test_parse_http_header_commands_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="expected 'Name: command'"):
        parse_http_header_commands(["not-a-header"])


def test_resolve_http_headers_runs_header_commands() -> None:
    command = f"{sys.executable} -c 'print(\"dynamic\")'"
    headers = resolve_http_headers(
        {"X-Static": "static"},
        {"X-Dynamic": command},
    )

    assert headers == {"X-Static": "static", "X-Dynamic": "dynamic"}


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("'", "Invalid command"),
        ("", "is empty"),
        ("/definitely/missing/matrix-mcp-header-token", "Failed to run"),
        (f"{sys.executable} -c 'import sys; print(\"bad\", file=sys.stderr); sys.exit(2)'", "bad"),
        (f"{sys.executable} -c ''", "produced no output"),
    ],
)
def test_resolve_http_headers_reports_header_command_failures(
    command: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        resolve_http_headers(http_header_commands={"X-Dynamic": command})


@pytest.mark.asyncio
async def test_login_with_password_uses_matrix_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        instances: ClassVar[list[FakeAsyncClient]] = []

        def __init__(
            self,
            homeserver: str,
            user: str,
            *,
            config: AsyncClientConfig,
        ) -> None:
            self.homeserver = homeserver
            self.user = user
            self.config = config
            self.closed = False
            FakeAsyncClient.instances.append(self)

        async def login(self, *, password: str, device_name: str) -> LoginResponse:
            assert password == "secret"
            assert device_name == "matrix-mcp-test"
            return LoginResponse(
                "@alice:example.com",
                "TESTDEVICE",
                "test-access-token",
            )

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("matrix_mcp.auth.AsyncClient", FakeAsyncClient)

    result = await login_with_password(
        homeserver="https://matrix.example.com/",
        user="@alice:example.com",
        password="secret",
        device_name="matrix-mcp-test",
        header_config=HTTPHeaderConfig(headers={"X-Access": "secret"}),
    )

    assert result.homeserver == "https://matrix.example.com"
    assert result.user_id == "@alice:example.com"
    assert result.device_id == "TESTDEVICE"
    assert result.access_token == "test-access-token"
    assert result.http_headers == {"X-Access": "secret"}
    assert len(FakeAsyncClient.instances) == 1
    assert FakeAsyncClient.instances[0].homeserver == "https://matrix.example.com"
    assert FakeAsyncClient.instances[0].user == "@alice:example.com"
    assert FakeAsyncClient.instances[0].config.custom_headers == {"X-Access": "secret"}
    assert FakeAsyncClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_login_with_token_fetches_user_id_when_login_response_omits_it() -> None:
    requests: list[tuple[str, str]] = []
    header_command = f"{sys.executable} -c 'print(\"command-secret\")'"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/_matrix/client/v3/login":
            assert request.headers["x-extra"] == "secret"
            assert request.headers["x-command"] == "command-secret"
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access-token",
                    "device_id": "TESTDEVICE",
                },
            )
        if request.url.path == "/_matrix/client/v3/account/whoami":
            assert request.headers["authorization"] == "Bearer test-access-token"
            assert request.headers["x-extra"] == "secret"
            assert request.headers["x-command"] == "command-secret"
            return httpx.Response(200, json={"user_id": "@alice:example.com"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await login_with_token(
            homeserver="https://matrix.example.com",
            login_token="test-login-token",
            header_config=HTTPHeaderConfig(
                headers={"X-Extra": "secret"},
                commands={"X-Command": header_command},
            ),
            http_client=http_client,
        )

    assert result.homeserver == "https://matrix.example.com"
    assert result.user_id == "@alice:example.com"
    assert result.device_id == "TESTDEVICE"
    assert result.access_token == "test-access-token"
    assert result.http_headers == {"X-Extra": "secret"}
    assert result.http_header_commands == {"X-Command": header_command}
    assert requests == [
        ("POST", "/_matrix/client/v3/login"),
        ("GET", "/_matrix/client/v3/account/whoami"),
    ]


@pytest.mark.asyncio
async def test_login_with_token_accepts_user_id_from_login_response() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "access_token": "test-access-token",
                "device_id": "TESTDEVICE",
                "user_id": "@alice:example.com",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await login_with_token(
            homeserver="https://matrix.example.com",
            login_token="test-login-token",
            http_client=http_client,
        )

    assert result.user_id == "@alice:example.com"
    assert result.device_id == "TESTDEVICE"
    assert result.access_token == "test-access-token"
    assert requests == [("POST", "/_matrix/client/v3/login")]


@pytest.mark.asyncio
async def test_login_with_token_reports_redirected_matrix_api() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                302, headers={"location": "https://gateway.example.com"}
            )
        )
    ) as http_client:
        with pytest.raises(RuntimeError, match="redirected"):
            await login_with_token(
                homeserver="https://matrix.example.com",
                login_token="test-login-token",
                http_client=http_client,
            )


@pytest.mark.asyncio
async def test_login_with_token_rejects_non_object_login_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[]))
    ) as http_client:
        with pytest.raises(RuntimeError, match="Matrix token login response"):
            await login_with_token(
                homeserver="https://matrix.example.com",
                login_token="test-login-token",
                http_client=http_client,
            )


@pytest.mark.asyncio
async def test_login_with_token_requires_access_token() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    ) as http_client:
        with pytest.raises(RuntimeError, match="access_token"):
            await login_with_token(
                homeserver="https://matrix.example.com",
                login_token="test-login-token",
                http_client=http_client,
            )
