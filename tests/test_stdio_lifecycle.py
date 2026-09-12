from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.id_state import MatrixIdStore
from matrix_mcp.matrix_client import MatrixAPIClient, NioMatrixDriver
from matrix_mcp.mcp_server import MatrixMCPTools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from matrix_mcp.mcp_server import MatrixMCPClient


ROOM = "!room:example.com"


@dataclass
class LifecycleServer:
    sessions: list[ClientSession] = field(default_factory=list)
    deny_requests: bool = False

    async def handle(self, request: web.Request) -> web.Response:
        await request.read()
        if self.deny_requests:
            return web.json_response(
                {"errcode": "M_FORBIDDEN", "error": "Request denied"}, status=403
            )
        path = request.path
        responses: dict[str, object] = {
            "joined_rooms": {"joined_rooms": []},
            "joined_members": {"joined": {}},
            "search": {"results": [], "limited": False},
            "messages": {"chunk": [], "start": "start"},
            "upload": {"content_uri": "mxc://example.com/report"},
        }
        if "/relations/" in path:
            return web.json_response({"chunk": []})
        if "/event/" in path:
            return web.json_response(
                {
                    "event_id": "$root",
                    "type": "m.room.message",
                    "sender": "@alice:example.com",
                    "origin_server_ts": 100,
                    "content": {"body": "Hello", "msgtype": "m.text"},
                }
            )
        if "/state/" in path or "/send/" in path:
            return web.json_response({"event_id": "$updated"})
        return web.json_response(responses.get(path.rsplit("/", 1)[-1], {}))


@pytest.fixture
async def lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[MatrixMCPConfig, LifecycleServer]]:
    server = LifecycleServer()

    def record_session(*args: Any, **kwargs: Any) -> ClientSession:
        session = ClientSession(*args, **kwargs)
        server.sessions.append(session)
        return session

    monkeypatch.setattr("nio.client.async_client.ClientSession", record_session)
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", server.handle)
    async with TestServer(app) as http_server:
        config = MatrixMCPConfig(
            homeserver=str(http_server.make_url("/")),
            user_id="@alice:example.com",
            device_id="TESTDEVICE",
            access_token="test-token",
        )
        try:
            yield config, server
        finally:
            for session in server.sessions:
                await session.close()


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("matrix_whoami", {}),
        ("matrix_list_rooms", {}),
        ("matrix_list_room_members", {"room_id": ROOM}),
        ("matrix_invite_user", {"room_id": ROOM, "user_id": "@bob:example.com"}),
        ("matrix_get_room_info", {"room_id": ROOM}),
        ("matrix_set_room_name", {"room_id": ROOM, "name": "Room"}),
        ("matrix_set_room_topic", {"room_id": ROOM, "topic": "Topic"}),
        ("matrix_set_room_avatar", {"room_id": ROOM, "avatar_url": ""}),
        ("matrix_get_profile", {}),
        ("matrix_set_display_name", {"displayname": "Alice"}),
        ("matrix_set_avatar", {"avatar_url": ""}),
        ("matrix_search_users", {"search_term": "Alice"}),
        ("matrix_read_room_recent", {"room_id": ROOM}),
        ("matrix_read_thread", {"room_id": ROOM, "thread_id": "$root"}),
        ("matrix_send_message", {"room_id": ROOM, "body": "Hello"}),
        ("matrix_send_message", {"room_id": ROOM, "file_path": "report.txt"}),
    ],
)
async def test_stdio_tools_close_owned_http_sessions_after_success(
    lifecycle: tuple[MatrixMCPConfig, LifecycleServer],
    tmp_path: Path,
    method: str,
    arguments: dict[str, object],
) -> None:
    config, server = lifecycle
    client = MatrixAPIClient(config=config, id_store=MatrixIdStore(tmp_path / "ids.json"))
    # Pre-open a real session so the local-only whoami tool also exercises cleanup.
    await client.list_rooms()
    assert len(server.sessions) == 1
    assert not server.sessions[0].closed
    tools = MatrixMCPTools(client_factory=lambda: client)
    if "file_path" in arguments:
        report = tmp_path / "report.txt"
        report.write_text("Report", encoding="utf-8")
        arguments = {**arguments, "file_path": str(report)}

    await getattr(tools, method)(**arguments)

    assert all(session.closed for session in server.sessions)


@pytest.mark.parametrize("method", ["matrix_list_rooms", "matrix_search_users"])
async def test_stdio_tools_close_owned_http_sessions_after_http_error(
    lifecycle: tuple[MatrixMCPConfig, LifecycleServer], tmp_path: Path, method: str
) -> None:
    config, server = lifecycle
    server.deny_requests = True
    tools = MatrixMCPTools(
        client_factory=lambda: MatrixAPIClient(
            config=config, id_store=MatrixIdStore(tmp_path / "ids.json")
        )
    )
    arguments = {"search_term": "Alice"} if method == "matrix_search_users" else {}

    with pytest.raises(RuntimeError, match="M_FORBIDDEN"):
        await getattr(tools, method)(**arguments)

    assert len(server.sessions) == 1
    assert server.sessions[0].closed


async def test_stdio_tools_preserve_injected_driver_ownership(
    lifecycle: tuple[MatrixMCPConfig, LifecycleServer],
) -> None:
    config, server = lifecycle
    driver = NioMatrixDriver(config)
    client = MatrixAPIClient(driver=driver)
    tools = MatrixMCPTools(client_factory=lambda: client)
    try:
        await tools.matrix_list_rooms()
        await client.aclose()
        assert len(server.sessions) == 1
        assert not server.sessions[0].closed
        assert await tools.matrix_list_rooms() == []
        assert len(server.sessions) == 1
    finally:
        await driver.aclose()


async def test_stdio_tools_preserve_custom_factory_client_ownership(
    lifecycle: tuple[MatrixMCPConfig, LifecycleServer],
) -> None:
    config, server = lifecycle
    driver = NioMatrixDriver(config)
    tools = MatrixMCPTools(client_factory=lambda: cast("MatrixMCPClient", driver))
    try:
        assert await tools.matrix_list_rooms() == []
        assert len(server.sessions) == 1
        assert not server.sessions[0].closed
        assert await tools.matrix_list_rooms() == []
        assert len(server.sessions) == 1
    finally:
        await driver.aclose()
