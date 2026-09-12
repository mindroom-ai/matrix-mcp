from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from aiohttp import web
from fastmcp import Client
from nio import AsyncClient

from matrix_mcp import hosted_auth, matrix_client
from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.hosted_auth import HostedSettings
from matrix_mcp.hosted_server import create_hosted_server
from matrix_mcp.id_state import MatrixIdStore
from matrix_mcp.mcp_server import create_mcp_server
from tests.hosted_helpers import CALLBACK, FakeMatrix, OAuthBrowser, PausedOAuthRequest

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
        yield OAuthBrowser(client, fake, app)


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


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize(
    ("registered", "unregistered"),
    [
        (CALLBACK, "https://other.example.com/callback"),
        ("http://127.0.0.1:8765/callback", "http://127.0.0.1:8766/callback"),
    ],
)
async def test_authorization_uses_only_registered_client_callbacks(
    settings: HostedSettings,
    matrix: tuple[FakeMatrix, str],
    registered: str,
    unregistered: str,
    *,
    restart: bool,
) -> None:
    configured = settings.model_copy(
        update={
            "allowed_client_redirect_uris": [registered, unregistered],
        }
    )
    async with browser_session(configured, matrix[0]) as browser:
        client_id = await browser.register(registered)
        if not restart:
            accepted = await browser.authorize(client_id, registered)
            rejected = await browser.authorize(client_id, unregistered)
    if restart:
        async with browser_session(configured, matrix[0]) as browser:
            accepted = await browser.authorize(client_id, registered)
            rejected = await browser.authorize(client_id, unregistered)
    assert accepted.status_code == 302
    assert rejected.status_code == 400
    assert "location" not in rejected.headers


async def test_removed_operator_callback_is_rejected_after_restart(
    settings: HostedSettings,
    matrix: tuple[FakeMatrix, str],
) -> None:
    async with browser_session(settings, matrix[0]) as browser:
        client_id = await browser.register()
    changed = settings.model_copy(
        update={
            "allowed_client_redirect_uris": ["https://other.example.com/callback"],
        }
    )
    async with browser_session(changed, matrix[0]) as browser:
        rejected = await browser.authorize(client_id)
    assert rejected.status_code == 400
    assert "location" not in rejected.headers


@pytest.mark.parametrize("phase", ["receive", "send"])
async def test_oauth_client_io_does_not_hold_mutation_lock(
    browser: OAuthBrowser,
    phase: Literal["receive", "send"],
) -> None:
    assert browser.asgi_app is not None
    request = PausedOAuthRequest(phase)
    task = asyncio.create_task(request.run(browser.asgi_app))
    try:
        await asyncio.wait_for(request.blocked.wait(), timeout=1)
        metadata, client_id = await asyncio.wait_for(
            asyncio.gather(
                browser.client.get("/.well-known/oauth-authorization-server"),
                browser.register(),
            ),
            timeout=1,
        )
        assert metadata.status_code == 200
        assert client_id
    finally:
        request.release.set()
        await asyncio.wait_for(task, timeout=1)


async def test_discovery_remains_available_during_mutation(browser: OAuthBrowser) -> None:
    client_id = await browser.register()
    tokens = await browser.login(client_id)
    browser.matrix.refresh_started = asyncio.Event()
    browser.matrix.refresh_release = asyncio.Event()
    task = asyncio.create_task(browser.refresh(client_id, tokens["refresh_token"]))
    try:
        await asyncio.wait_for(browser.matrix.refresh_started.wait(), timeout=1)
        metadata = await asyncio.wait_for(
            browser.client.get("/.well-known/oauth-authorization-server"),
            timeout=1,
        )
        assert metadata.status_code == 200
    finally:
        browser.matrix.refresh_release.set()
        response = await asyncio.wait_for(task, timeout=1)
    assert response.status_code == 200


@pytest.mark.parametrize("chunked", [False, True])
async def test_oauth_request_body_size_is_bounded(browser: OAuthBrowser, *, chunked: bool) -> None:
    payload = json.dumps(
        {
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "client_name": "x" * 65536,
        }
    ).encode()

    async def chunks() -> AsyncIterator[bytes]:
        yield payload[:32768]
        yield payload[32768:]

    response = await browser.client.post(
        "/register",
        content=chunks() if chunked else payload,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    await browser.register()


async def test_oauth_request_body_deadline_is_bounded(
    browser: OAuthBrowser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hosted_auth, "_AUTH_BODY_TIMEOUT_SECONDS", 0.02, raising=False)
    assert browser.asgi_app is not None
    request = PausedOAuthRequest("receive")
    task = asyncio.create_task(request.run(browser.asgi_app))
    try:
        await asyncio.wait_for(request.blocked.wait(), timeout=1)
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "OAuth body reception did not meet its deadline"
        assert request.messages[0]["status"] == 408
        await browser.register()
    finally:
        request.release.set()
        await asyncio.wait_for(task, timeout=1)


async def test_successive_refreshes_preserve_omitted_matrix_refresh_token(
    browser: OAuthBrowser,
) -> None:
    browser.matrix.omit_replacement_refresh = True
    client_id = await browser.register()
    tokens = await browser.login(client_id)
    for _ in range(3):
        browser.matrix.expired_access.update(browser.matrix.sessions)
        expired = await browser.rpc(tokens["access_token"], "tools/list", {})
        assert expired.status_code == 401
        renewed = await browser.refresh(client_id, tokens["refresh_token"])
        assert renewed.status_code == 200, renewed.text
        tokens = renewed.json()
        await browser.call(tokens["access_token"], "matrix_whoami", {})


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


@pytest.mark.parametrize("expiry", [None, "malformed-expiry", True, 3_600_000.5, 0, -1000])
async def test_invalid_matrix_expiry_rejects_callback_without_echoing_value(
    browser: OAuthBrowser, expiry: float | str | None
) -> None:
    browser.matrix.expires_in_ms = expiry
    callback = await browser.consent(await browser.register())
    response = await browser.client.get(
        callback + "&" + urlencode({"loginToken": browser.matrix.login_token("alice")})
    )
    assert response.status_code == 500
    assert "Matrix access token expiry is invalid" in response.text
    assert "malformed-expiry" not in response.text


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


async def test_revocation_during_whoami_rejects_pending_mcp_request(browser: OAuthBrowser) -> None:
    client_id = await browser.register()
    tokens = await browser.login(client_id)
    browser.matrix.logout_fails = True
    browser.matrix.whoami_started = asyncio.Event()
    browser.matrix.whoami_release = asyncio.Event()
    pending = asyncio.create_task(browser.rpc(tokens["access_token"], "tools/list", {}))
    try:
        await asyncio.wait_for(browser.matrix.whoami_started.wait(), timeout=1)
        await asyncio.wait_for(browser.revoke(client_id, tokens["refresh_token"]), timeout=1)
        assert browser.matrix.sessions  # Failed logout leaves the remote token valid.
    finally:
        browser.matrix.whoami_release.set()
        response = await asyncio.wait_for(pending, timeout=1)
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
                    "mentions": ["@helper:example.com", "@reader:example.com"],
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
        assert message["content"]["m.mentions"] == {
            "user_ids": ["@helper:example.com", "@reader:example.com"]
        }
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
    for tool in tools:
        properties = tool["inputSchema"]["properties"]
        assert not ({"file_path", "homeserver", "access_token", "http_url"} & properties.keys())
        if tool["name"] == "matrix_send_message":
            assert not ({"filename", "content_type"} & properties.keys())
        if tool["name"] not in {"matrix_invite_user", "matrix_get_profile"}:
            assert "user_id" not in properties
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


async def test_two_users_conversation_tools_keep_request_identity(browser: OAuthBrowser) -> None:
    alice = await browser.login(await browser.register(), "alice")
    bob = await browser.login(await browser.register(), "bob")

    for user, tokens in (("alice", alice), ("bob", bob)):
        access = tokens["access_token"]
        history = await browser.call(
            access,
            "matrix_read_history",
            {"room_id": "!v12hash", "limit": 5},
        )
        assert history["structuredContent"]["events"][0]["body"] == "Hello"
        await browser.call(
            access,
            "matrix_reply",
            {"room_id": "!v12hash", "event_id": "$root", "body": f"reply from {user}"},
        )
        await browser.call(
            access,
            "matrix_react",
            {"room_id": "!v12hash", "event_id": "$root", "key": user},
        )
        joined = await browser.call(
            access,
            "matrix_join_room",
            {"room_id_or_alias": "#general:example.com"},
        )
        assert joined["structuredContent"]["room_id"] == "!joined:example.com"
        uploaded = await browser.call(
            access,
            "matrix_upload_media",
            {
                "data_base64": base64.b64encode(user.encode()).decode(),
                "filename": f"{user}.txt",
                "content_type": "text/plain",
            },
        )
        media_url = uploaded["structuredContent"]["content_uri"]
        downloaded = await browser.call(
            access,
            "matrix_download_media",
            {"media_url": media_url},
        )
        assert base64.b64decode(downloaded["structuredContent"]["data_base64"]).decode() == user
        await browser.call(
            access,
            "matrix_send_media",
            {
                "room_id": "!v12hash",
                "media_url": media_url,
                "filename": f"{user}.txt",
                "content_type": "text/plain",
                "size": len(user),
            },
        )

    for operation in ("history", "reply", "react", "join", "upload", "download", "send_media"):
        assert {
            request["actor"]
            for request in browser.matrix.conversation_requests
            if request["operation"] == operation
        } == {"@alice:example.com", "@bob:example.com"}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("matrix_read_history", {"room_id": "not-a-room"}),
        ("matrix_get_event_context", {"room_id": "!v12hash", "event_id": "event"}),
        ("matrix_reply", {"room_id": "!v12hash", "event_id": "$event", "body": ""}),
        ("matrix_join_room", {"room_id_or_alias": "general"}),
        ("matrix_get_unread", {"limit": 0}),
        ("matrix_upload_media", {"data_base64": "%%%", "filename": "bad.txt"}),
        ("matrix_download_media", {"media_url": "https://example.com/media"}),
        (
            "matrix_send_media",
            {
                "room_id": "!v12hash",
                "media_url": "mxc://example.com/media",
                "filename": "x.txt",
                "size": -1,
            },
        ),
    ],
)
async def test_hosted_conversation_tools_reject_malformed_input(
    browser: OAuthBrowser,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    tokens = await browser.login(await browser.register())
    response = await browser.rpc(
        tokens["access_token"],
        "tools/call",
        {"name": tool, "arguments": arguments},
    )
    assert response.json()["result"]["isError"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("matrix_read_history", {}),
        ("matrix_reply", {"event_id": "$root", "body": "denied"}),
    ],
)
async def test_hosted_conversation_tools_surface_room_denials(
    browser: OAuthBrowser,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    tokens = await browser.login(await browser.register())
    response = await browser.rpc(
        tokens["access_token"],
        "tools/call",
        {
            "name": tool,
            "arguments": {"room_id": "!forbidden:example.com", **arguments},
        },
    )
    result = response.json()["result"]
    assert result["isError"]
    assert "M_FORBIDDEN" in str(result)


async def test_room_profile_tools_use_each_callers_matrix_identity(browser: OAuthBrowser) -> None:
    alice = await browser.login(await browser.register(), "alice")
    bob = await browser.login(await browser.register(), "bob")
    room = {"room_id": "!room:example.com"}
    for user, tokens in [("alice", alice), ("bob", bob)]:
        access = tokens["access_token"]
        profile = await browser.call(access, "matrix_get_profile", {})
        assert profile["structuredContent"]["user_id"] == f"@{user}:example.com"
        await browser.call(access, "matrix_set_display_name", {"displayname": user.title()})
        await browser.call(access, "matrix_set_avatar", {"avatar_url": f"mxc://example.com/{user}"})
        await browser.call(
            access, "matrix_invite_user", {**room, "user_id": f"@{user}-friend:example.com"}
        )
        for tool, arguments in [
            ("matrix_set_room_name", {"name": f"{user}'s room"}),
            ("matrix_set_room_topic", {"topic": f"Topic from {user}"}),
            ("matrix_set_room_avatar", {"avatar_url": f"mxc://example.com/{user}"}),
        ]:
            result = await browser.call(access, tool, {**room, **arguments})
            assert result["structuredContent"]["event_id"].startswith("$state")
    assert browser.matrix.profiles == {
        "@alice:example.com": {"displayname": "Alice", "avatar_url": "mxc://example.com/alice"},
        "@bob:example.com": {"displayname": "Bob", "avatar_url": "mxc://example.com/bob"},
    }
    assert browser.matrix.invitations == [
        {
            "actor": "@alice:example.com",
            "room_id": "!room:example.com",
            "user_id": "@alice-friend:example.com",
        },
        {
            "actor": "@bob:example.com",
            "room_id": "!room:example.com",
            "user_id": "@bob-friend:example.com",
        },
    ]
    assert [change["actor"] for change in browser.matrix.room_writes] == [
        "@alice:example.com",
        "@alice:example.com",
        "@alice:example.com",
        "@bob:example.com",
        "@bob:example.com",
        "@bob:example.com",
    ]
    info = await browser.call(alice["access_token"], "matrix_get_room_info", room)
    assert info["structuredContent"] == {
        "id": None,
        "room_id": "!room:example.com",
        "name": "bob's room",
        "topic": "Topic from bob",
        "avatar_url": "mxc://example.com/bob",
    }
    members = await browser.call(
        alice["access_token"], "matrix_list_room_members", {**room, "limit": 1}
    )
    assert members["structuredContent"] == {
        "members": [
            {
                "user_id": "@alice:example.com",
                "displayname": "Alice",
                "avatar_url": "mxc://example.com/alice",
            }
        ],
        "total": 2,
        "next_offset": 1,
    }
    second = await browser.call(
        alice["access_token"], "matrix_list_room_members", {**room, "limit": 1, "offset": 1}
    )
    assert second["structuredContent"]["members"][0]["user_id"] == "@bob:example.com"
    assert second["structuredContent"]["next_offset"] is None
    search = await browser.call(
        alice["access_token"], "matrix_search_users", {"search_term": "bob", "limit": 1}
    )
    assert search["structuredContent"] == {
        "results": [
            {
                "user_id": "@bob:example.com",
                "displayname": "Bob",
                "avatar_url": "mxc://example.com/bob",
            }
        ],
        "limited": False,
    }


async def test_stdio_room_tools_resolve_numeric_refs(
    matrix: tuple[FakeMatrix, str], tmp_path: Path
) -> None:
    tokens = matrix[0].session("alice")
    config = MatrixMCPConfig(
        homeserver=matrix[1],
        **{key: tokens[key] for key in ("access_token", "user_id", "device_id")},
    )
    driver = matrix_client.NioMatrixDriver(config)
    api = matrix_client.MatrixAPIClient(
        driver=driver, id_store=MatrixIdStore(tmp_path / "ids.json")
    )
    try:
        async with Client(create_mcp_server(client_factory=lambda: api)) as client:
            rooms = await client.call_tool("matrix_list_rooms", {})
            assert rooms.structured_content is not None
            room_ref = rooms.structured_content["result"][0]["id"]
            await client.call_tool(
                "matrix_invite_user",
                {
                    "room_id": room_ref,
                    "user_id": "@bob:example.com",
                },
            )
            await client.call_tool(
                "matrix_set_room_topic",
                {
                    "room_id": str(room_ref),
                    "topic": "Updated by room ref",
                },
            )
            info = await client.call_tool("matrix_get_room_info", {"room_id": room_ref})
            assert info.structured_content is not None
            assert info.structured_content["topic"] == "Updated by room ref"
            assert info.structured_content["id"] == room_ref
            assert info.structured_content["room_id"] == "!room:example.com"
            members = await client.call_tool("matrix_list_room_members", {"room_id": room_ref})
            assert members.structured_content is not None
            assert members.structured_content["members"][0]["user_id"] == "@alice:example.com"
        assert matrix[0].invitations == [
            {
                "actor": "@alice:example.com",
                "room_id": "!room:example.com",
                "user_id": "@bob:example.com",
            }
        ]
    finally:
        await driver.close()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("matrix_list_room_members", {}),
        ("matrix_get_room_info", {}),
        ("matrix_invite_user", {"user_id": "@friend:example.com"}),
        ("matrix_set_room_name", {"name": "Denied"}),
        ("matrix_set_room_topic", {"topic": "Denied"}),
        ("matrix_set_room_avatar", {"avatar_url": "mxc://example.com/avatar"}),
    ],
)
async def test_room_tools_surface_homeserver_permission_denials(
    browser: OAuthBrowser, tool: str, arguments: dict[str, str]
) -> None:
    tokens = await browser.login(await browser.register())
    response = await browser.rpc(
        tokens["access_token"],
        "tools/call",
        {
            "name": tool,
            "arguments": {"room_id": "!forbidden:example.com", **arguments},
        },
    )
    result = response.json()["result"]
    assert result["isError"]
    assert "M_FORBIDDEN" in str(result) or "Room access denied" in str(result)
    assert browser.matrix.invitations == []
    assert browser.matrix.room_writes == []


async def test_room_profile_tool_hints_and_input_boundaries(browser: OAuthBrowser) -> None:
    tokens = await browser.login(await browser.register())
    access = tokens["access_token"]
    response = await browser.rpc(access, "tools/list", {})
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    for name in (
        "matrix_list_room_members",
        "matrix_get_room_info",
        "matrix_get_profile",
        "matrix_search_users",
    ):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    for name in (
        "matrix_invite_user",
        "matrix_set_room_name",
        "matrix_set_room_topic",
        "matrix_set_room_avatar",
        "matrix_set_display_name",
        "matrix_set_avatar",
    ):
        assert tools[name]["annotations"]["readOnlyHint"] is False
    assert tools["matrix_invite_user"]["annotations"]["destructiveHint"] is False
    assert tools["matrix_set_room_avatar"]["annotations"]["destructiveHint"] is True
    for name, arguments in [
        ("matrix_invite_user", {"room_id": "12", "user_id": "@bob:example.com"}),
        ("matrix_invite_user", {"room_id": "!room:example.com", "user_id": "bob"}),
        ("matrix_list_room_members", {"room_id": "!room:example.com", "limit": 0}),
        ("matrix_list_room_members", {"room_id": "!room:example.com", "offset": -1}),
        ("matrix_set_avatar", {"avatar_url": "https://example.com/avatar.png"}),
        ("matrix_search_users", {"search_term": " "}),
    ]:
        result = await browser.rpc(access, "tools/call", {"name": name, "arguments": arguments})
        assert result.json()["result"]["isError"]
    assert browser.matrix.invitations == []
    assert browser.matrix.room_writes == []
    assert browser.matrix.profiles["@alice:example.com"]["avatar_url"] is None


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
