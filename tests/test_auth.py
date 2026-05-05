from __future__ import annotations

import pytest

from matrix_mcp.auth import build_sso_redirect_url, extract_login_token


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
