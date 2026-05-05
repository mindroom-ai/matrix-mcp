from __future__ import annotations

import httpx
import pytest

from matrix_mcp.auth import (
    SSOProvider,
    build_sso_redirect_url,
    extract_login_token,
    login_with_token,
    parse_sso_providers,
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


@pytest.mark.asyncio
async def test_login_with_token_fetches_user_id_when_login_response_omits_it() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/_matrix/client/v3/login":
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access-token",
                    "device_id": "TESTDEVICE",
                },
            )
        if request.url.path == "/_matrix/client/v3/account/whoami":
            assert request.headers["authorization"] == "Bearer test-access-token"
            return httpx.Response(200, json={"user_id": "@alice:example.com"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await login_with_token(
            homeserver="https://matrix.example.com",
            login_token="test-login-token",
            http_client=http_client,
        )

    assert result.homeserver == "https://matrix.example.com"
    assert result.user_id == "@alice:example.com"
    assert result.device_id == "TESTDEVICE"
    assert result.access_token == "test-access-token"
    assert requests == [
        ("POST", "/_matrix/client/v3/login"),
        ("GET", "/_matrix/client/v3/account/whoami"),
    ]
