from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from aiohttp import web
from nio import AsyncClient

from matrix_mcp import matrix_client
from matrix_mcp.hosted_auth import HostedSettings
from matrix_mcp.hosted_server import create_hosted_server
from tests.hosted_helpers import CALLBACK, FakeMatrix, OAuthBrowser

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def matrix() -> AsyncIterator[tuple[FakeMatrix, str]]:
    fake = FakeMatrix()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        site = web.SockSite(runner, sock)
        await site.start()
        try:
            yield fake, f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            await runner.cleanup()


@pytest.fixture
def settings(tmp_path: Path, matrix: tuple[FakeMatrix, str]) -> HostedSettings:
    return HostedSettings(
        public_base_url="http://127.0.0.1:8000",
        homeserver="https://matrix.example.com",
        api_base_url=matrix[1],
        state_directory=tmp_path,
        secret_key="a-stable-test-key-with-at-least-32-characters",
        allowed_client_redirect_uris=[CALLBACK],
    )


@asynccontextmanager
async def browser_session(
    settings: HostedSettings, fake: FakeMatrix
) -> AsyncIterator[OAuthBrowser]:
    app = create_hosted_server(settings).http_app(json_response=True, stateless_http=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=settings.public_base_url,
        ) as client,
    ):
        yield OAuthBrowser(client, fake)


@pytest.fixture
async def browser(
    settings: HostedSettings, matrix: tuple[FakeMatrix, str]
) -> AsyncIterator[OAuthBrowser]:
    ready: asyncio.Future[OAuthBrowser] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def run() -> None:
        try:
            async with browser_session(settings, matrix[0]) as result:
                ready.set_result(result)
                await stop.wait()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    task = asyncio.create_task(run())
    try:
        yield await ready
    finally:
        stop.set()
        await task


async def test_http_mcp_requires_auth(tmp_path: Path) -> None:
    settings = HostedSettings(
        public_base_url="http://127.0.0.1:8000",
        homeserver="https://matrix.example.com",
        state_directory=tmp_path,
        secret_key="a-stable-test-key-with-at-least-32-characters",
        allowed_client_redirect_uris=["https://client.example.com/callback"],
    )
    app = create_hosted_server(settings).http_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=settings.public_base_url
        ) as client,
    ):
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]


async def test_discovery_and_redirect_registration(browser: OAuthBrowser) -> None:
    metadata = await browser.client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    assert metadata.json()["scopes_supported"] == ["matrix"]
    assert metadata.json()["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata.json()["revocation_endpoint_auth_methods_supported"] == ["none"]
    resource = await browser.client.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.status_code == 200
    assert resource.json()["resource"] == "http://127.0.0.1:8000/mcp"
    await browser.register()
    for uri in ["https://untrusted.example.com/callback", "http://127.0.0.1:1234/callback"]:
        response = await browser.client.post(
            "/register",
            json={
                "redirect_uris": [uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert response.status_code == 400


async def test_registration_preserves_trailing_slash_in_callback(
    settings: HostedSettings,
    matrix: tuple[FakeMatrix, str],
) -> None:
    values = settings.model_dump()
    values.update(secret_key=settings.secret_key, allowed_client_redirect_uris=[f"{CALLBACK}/"])
    configured = HostedSettings(**values)
    async with browser_session(configured, matrix[0]) as browser:
        await browser.register(f"{CALLBACK}/")


async def test_callback_requires_state_login_token_and_bound_browser(browser: OAuthBrowser) -> None:
    client_id = await browser.register()
    callback = await browser.consent(client_id)
    state = parse_qs(urlsplit(callback).query)["state"][0]
    token = browser.matrix.login_token("alice")
    for params in [
        {"loginToken": token},
        {"state": state},
        {"state": "wrong", "loginToken": token},
        {"state": state, "code": token},
    ]:
        response = await browser.client.get("/auth/callback", params=params)
        assert response.status_code == 400
    cookies = httpx.Cookies(browser.client.cookies)
    browser.client.cookies.clear()
    response = await browser.client.get(callback, params={"state": state, "loginToken": token})
    assert response.status_code == 403
    browser.client.cookies.update(cookies)
    responses = await asyncio.gather(
        *[
            browser.client.get("/auth/callback", params={"state": state, "loginToken": token})
            for _ in range(2)
        ]
    )
    assert sorted(response.status_code for response in responses) == [302, 400]


async def test_pkce_and_concurrent_code_replay(browser: OAuthBrowser) -> None:
    client_id = await browser.register()
    code = await browser.code(client_id)
    response = await browser.exchange(client_id, code, "incorrect-verifier")
    assert response.status_code in (400, 401)
    responses = await asyncio.gather(*[browser.exchange(client_id, code) for _ in range(2)])
    assert sum(response.status_code == 200 for response in responses) == 1
    assert sorted(response.status_code for response in responses)[1] in (400, 401)


@pytest.mark.parametrize("legacy", [False, True])
async def test_refresh_rotation_cross_client_and_lineage_revoke(
    browser: OAuthBrowser, *, legacy: bool
) -> None:
    browser.matrix.legacy = legacy
    owner = await browser.register()
    other = await browser.register()
    first = await browser.login(owner)
    denied = await browser.refresh(other, first["refresh_token"])
    assert denied.status_code in (400, 401)
    responses = await asyncio.gather(
        *[browser.refresh(owner, first["refresh_token"]) for _ in range(2)]
    )
    assert sum(response.status_code == 200 for response in responses) == 1
    second = next(response.json() for response in responses if response.status_code == 200)
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    await browser.revoke(other, second["access_token"])
    await browser.revoke(other, second["refresh_token"])
    await browser.call(second["access_token"], "matrix_whoami", {})
    browser.matrix.logout_fails = True
    await browser.revoke(owner, first["access_token"])
    for tokens in [first, second]:
        response = await browser.rpc(tokens["access_token"], "tools/list", {})
        assert response.status_code == 401
        response = await browser.refresh(owner, tokens["refresh_token"])
        assert response.status_code in (400, 401)


@pytest.mark.parametrize("legacy", [False, True])
async def test_remote_invalidation_rejects_access_and_refresh(
    browser: OAuthBrowser, *, legacy: bool
) -> None:
    browser.matrix.legacy = legacy
    client_id = await browser.register()
    tokens = await browser.login(client_id)
    browser.matrix.invalidate("alice")
    response = await browser.rpc(tokens["access_token"], "tools/list", {})
    assert response.status_code == 401
    response = await browser.refresh(client_id, tokens["refresh_token"])
    assert response.status_code in (400, 401)


async def test_restart_keeps_encrypted_credentials(
    settings: HostedSettings, matrix: tuple[FakeMatrix, str]
) -> None:
    async with browser_session(settings, matrix[0]) as browser:
        client_id = await browser.register()
        tokens = await browser.login(client_id)
    contents = b"".join(
        path.read_bytes() for path in settings.state_directory.rglob("*") if path.is_file()
    )
    for secret in [
        *matrix[0].sessions,
        *matrix[0].refreshes,
        "@alice:example.com",
        tokens["refresh_token"],
    ]:
        assert secret.encode() not in contents
    async with browser_session(settings, matrix[0]) as browser:
        await browser.call(tokens["access_token"], "matrix_whoami", {})
        response = await browser.refresh(client_id, tokens["refresh_token"])
        assert response.status_code == 200


async def test_second_process_and_wrong_key_fail_closed(
    settings: HostedSettings, matrix: tuple[FakeMatrix, str]
) -> None:
    async with browser_session(settings, matrix[0]):
        with pytest.raises(RuntimeError, match="already in use"):
            async with browser_session(settings, matrix[0]):
                pytest.fail("second server started")
    changed = settings.model_copy(
        update={"secret_key": "different-stable-test-key-with-32-characters"}
    )
    with pytest.raises(RuntimeError, match=r"key|configuration"):
        async with browser_session(changed, matrix[0]):
            pytest.fail("wrong key accepted")


async def test_expired_mcp_access_is_rejected(browser: OAuthBrowser) -> None:
    browser.matrix.expires_in_ms = 1000
    client_id = await browser.register()
    tokens = await browser.login(client_id)
    await asyncio.sleep(1.2)
    response = await browser.rpc(tokens["access_token"], "tools/list", {})
    assert response.status_code == 401
    response = await browser.refresh(client_id, tokens["refresh_token"])
    assert response.status_code == 200


async def test_public_client_can_revoke_without_a_client_secret(browser: OAuthBrowser) -> None:
    client_id = await browser.register()
    tokens = await browser.login(client_id)
    response = await browser.client.post(
        "/revoke",
        data={
            "client_id": client_id,
            "token": tokens["refresh_token"],
        },
    )
    assert response.status_code == 200, response.text
    assert browser.matrix.sessions == {}
    response = await browser.rpc(tokens["access_token"], "tools/list", {})
    assert response.status_code == 401


async def test_revocation_racing_refresh_cannot_revive_session(browser: OAuthBrowser) -> None:
    client_id = await browser.register()
    tokens = await browser.login(client_id)
    results = await asyncio.gather(
        browser.refresh(client_id, tokens["refresh_token"]),
        browser.revoke(client_id, tokens["access_token"]),
    )
    refreshed = results[0]
    assert refreshed is not None
    if refreshed.status_code == 200:
        response = await browser.rpc(refreshed.json()["access_token"], "tools/list", {})
        assert response.status_code == 401
        response = await browser.refresh(client_id, refreshed.json()["refresh_token"])
        assert response.status_code in (400, 401)
    response = await browser.rpc(tokens["access_token"], "tools/list", {})
    assert response.status_code == 401


async def test_two_users_tools_use_request_identity_without_local_files(
    browser: OAuthBrowser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    alice = await browser.login(await browser.register(), "alice")
    bob = await browser.login(await browser.register(), "bob")
    results = await asyncio.gather(
        *[
            browser.call(
                tokens["access_token"],
                "matrix_send_message",
                {
                    "room_id": "!room:example.com",
                    "body": name,
                    "thread_id": "$root",
                },
            )
            for name, tokens in [("alice", alice), ("bob", bob)]
        ]
    )
    assert len(results) == 2
    assert {message["sender"] for message in browser.matrix.messages} == {
        "@alice:example.com",
        "@bob:example.com",
    }
    for message in browser.matrix.messages:
        assert message["sender"] == f"@{message['content']['body']}:example.com"
        assert message["content"]["m.relates_to"]["event_id"] == "$root"
    for name, args in [
        ("matrix_list_rooms", {}),
        ("matrix_read_room_recent", {"room_id": "!room:example.com"}),
        ("matrix_read_thread", {"room_id": "!room:example.com", "thread_id": "$root"}),
    ]:
        result = await browser.call(alice["access_token"], name, args)
        assert result
    response = await browser.rpc(alice["access_token"], "tools/list", {})
    tools = response.json()["result"]["tools"]
    assert len(tools) == 5
    for tool in tools:
        properties = tool["inputSchema"]["properties"]
        assert not (
            {"file_path", "filename", "content_type", "user_id", "homeserver"} & properties.keys()
        )
        if "room_id" in properties:
            assert properties["room_id"]["type"] == "string"
    response = await browser.rpc(
        alice["access_token"],
        "tools/call",
        {
            "name": "matrix_send_message",
            "arguments": {"room_id": "12", "body": "bad ref"},
        },
    )
    assert response.json()["result"]["isError"]
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("encryption_status", [200, 403, 500])
async def test_encrypted_or_unknown_room_never_receives_plaintext(
    browser: OAuthBrowser, encryption_status: int
) -> None:
    browser.matrix.encryption_status = encryption_status
    tokens = await browser.login(await browser.register())
    response = await browser.rpc(
        tokens["access_token"],
        "tools/call",
        {
            "name": "matrix_send_message",
            "arguments": {"room_id": "!room:example.com", "body": "private text"},
        },
    )
    assert response.json()["result"]["isError"]
    assert browser.matrix.messages == []


async def test_hosted_tool_closes_real_matrix_clients(
    browser: OAuthBrowser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[AsyncClient] = []

    def track_client(*args: Any, **kwargs: Any) -> AsyncClient:
        client = AsyncClient(*args, **kwargs)
        opened.append(client)
        return client

    monkeypatch.setattr(matrix_client, "AsyncClient", track_client)
    tokens = await browser.login(await browser.register())
    await browser.call(tokens["access_token"], "matrix_list_rooms", {})
    assert opened
    assert all(client.client_session is None or client.client_session.closed for client in opened)
