from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.id_state import MatrixIdStore
from matrix_mcp.matrix_client import MatrixAPIClient, NioMatrixDriver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


ROOM = "!room:example.com"
ROOM_PATH = f"/_matrix/client/v3/rooms/{ROOM}"
PROFILE_PATH = "/_matrix/client/v3/profile/@alice:example.com"
SEARCH_PATH = "/_matrix/client/v3/user_directory/search"


@dataclass
class MatrixEndpoint:
    responses: dict[tuple[str, str], tuple[object, int]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "headers": dict(request.headers),
                "body": await request.json() if request.can_read_body else None,
            }
        )
        data, status = self.responses.get(
            (request.method, request.path),
            ({"errcode": "M_NOT_FOUND", "error": "Not found"}, 404),
        )
        return web.json_response(data, status=status)


@pytest.fixture
async def matrix() -> AsyncIterator[tuple[NioMatrixDriver, MatrixEndpoint]]:
    endpoint = MatrixEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        driver = NioMatrixDriver(
            MatrixMCPConfig(
                homeserver=str(server.make_url("/")),
                user_id="@alice:example.com",
                device_id="TESTDEVICE",
                access_token="test-token",
                http_headers={"X-Example": "custom-value"},
            )
        )
        try:
            yield driver, endpoint
        finally:
            await driver.aclose()


@pytest.mark.parametrize(
    ("offset", "expected_users", "next_offset"),
    [
        (0, ["@alice:example.com", "@bob:example.com"], 2),
        (2, ["@carol:example.com"], None),
        (3, [], None),
    ],
)
async def test_joined_members_are_sorted_and_paginated(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint],
    offset: int,
    expected_users: list[str],
    next_offset: int | None,
) -> None:
    driver, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/joined_members")] = (
        {
            "joined": {
                "@carol:example.com": {},
                "@alice:example.com": {
                    "display_name": "Alice",
                    "avatar_url": "mxc://example.com/alice",
                },
                "@bob:example.com": {"display_name": None, "avatar_url": None},
            }
        },
        200,
    )

    page = await driver.list_room_members(ROOM, limit=2, offset=offset)

    assert [member.user_id for member in page.members] == expected_users
    assert page.total == 3
    assert page.next_offset == next_offset
    if offset == 0:
        assert page.members[0].displayname == "Alice"
        assert page.members[0].avatar_url == "mxc://example.com/alice"
        assert page.members[1].displayname is None


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (101, 0), (1, -1)])
async def test_member_page_bounds_reject_before_http(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], limit: int, offset: int
) -> None:
    driver, endpoint = matrix
    with pytest.raises(ValueError, match=r"limit|offset"):
        await driver.list_room_members(ROOM, limit=limit, offset=offset)
    assert endpoint.requests == []


async def test_room_info_reads_all_fields_and_keeps_numeric_reference(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], tmp_path: Path
) -> None:
    driver, endpoint = matrix
    for event_type, content in [
        ("m.room.name", {"name": "General"}),
        ("m.room.topic", {"topic": "Room topic"}),
        ("m.room.avatar", {"url": "mxc://example.com/room"}),
    ]:
        endpoint.responses[("GET", f"{ROOM_PATH}/state/{event_type}")] = (content, 200)
    ids = MatrixIdStore(tmp_path / "ids.json")
    client = MatrixAPIClient(driver=driver, id_store=ids)

    first = await client.get_room_info(ROOM)
    second = await client.get_room_info(1)

    assert first.model_dump() == {
        "id": 1,
        "room_id": ROOM,
        "name": "General",
        "topic": "Room topic",
        "avatar_url": "mxc://example.com/room",
    }
    assert second == first


async def test_room_info_missing_state_is_unset(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint],
) -> None:
    driver, _ = matrix
    info = await driver.get_room_info(ROOM)
    assert info.name is None
    assert info.topic is None
    assert info.avatar_url is None


@pytest.mark.parametrize("event_type", ["m.room.name", "m.room.topic", "m.room.avatar"])
@pytest.mark.parametrize("error", ["M_FORBIDDEN", "M_UNKNOWN_TOKEN", "M_UNKNOWN"])
async def test_room_info_does_not_hide_state_errors(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], event_type: str, error: str
) -> None:
    driver, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/state/{event_type}")] = (
        {"errcode": error, "error": "Request denied"},
        403,
    )
    with pytest.raises(RuntimeError, match=error):
        await driver.get_room_info(ROOM)


@pytest.mark.parametrize(
    ("method", "event_type", "key", "value"),
    [
        ("set_room_name", "m.room.name", "name", "Renamed"),
        ("set_room_name", "m.room.name", "name", ""),
        ("set_room_topic", "m.room.topic", "topic", "New topic"),
        ("set_room_topic", "m.room.topic", "topic", ""),
        ("set_room_avatar", "m.room.avatar", "url", "mxc://example.com/room"),
        ("set_room_avatar", "m.room.avatar", "url", ""),
    ],
)
async def test_room_setters_write_only_fixed_state_with_numeric_refs(  # noqa: PLR0913 - Parameterized HTTP contract.
    matrix: tuple[NioMatrixDriver, MatrixEndpoint],
    tmp_path: Path,
    method: str,
    event_type: str,
    key: str,
    value: str,
) -> None:
    driver, endpoint = matrix
    path = f"{ROOM_PATH}/state/{event_type}"
    endpoint.responses[("PUT", path)] = ({"event_id": "$updated"}, 200)
    ids = MatrixIdStore(tmp_path / "ids.json")
    ids.room_ref(ROOM)
    client = MatrixAPIClient(driver=driver, id_store=ids)

    assert await getattr(client, method)(1, value) == "$updated"
    assert endpoint.requests[0]["path"] == path
    assert endpoint.requests[0]["body"] == {key: value}


async def test_invite_and_members_resolve_numeric_room_refs(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], tmp_path: Path
) -> None:
    driver, endpoint = matrix
    endpoint.responses[("POST", f"{ROOM_PATH}/invite")] = ({}, 200)
    endpoint.responses[("GET", f"{ROOM_PATH}/joined_members")] = ({"joined": {}}, 200)
    ids = MatrixIdStore(tmp_path / "ids.json")
    ids.room_ref(ROOM)
    client = MatrixAPIClient(driver=driver, id_store=ids)

    await client.invite_user("1", "@bob:example.com")
    assert (await client.list_room_members(1)).total == 0
    assert endpoint.requests[0]["body"] == {"user_id": "@bob:example.com"}
    assert endpoint.requests[1]["path"] == f"{ROOM_PATH}/joined_members"


@pytest.mark.parametrize(
    "user_id",
    [
        "bob",
        "bob:example.com",
        "@:example.com",
        "@bob:",
        "@bob:example.com/path",
        "@bob:example.com\n",
    ],
)
@pytest.mark.parametrize("method", ["invite_user", "get_profile"])
async def test_invalid_user_ids_reject_before_http(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], method: str, user_id: str
) -> None:
    driver, endpoint = matrix
    args = (ROOM, user_id) if method == "invite_user" else (user_id,)
    with pytest.raises(ValueError, match="Invalid Matrix user ID"):
        await getattr(driver, method)(*args)
    assert endpoint.requests == []


@pytest.mark.parametrize("user_id", [None, "@bob:example.com"])
async def test_profile_defaults_to_configured_user_and_preserves_optional_fields(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], user_id: str | None
) -> None:
    driver, endpoint = matrix
    target = user_id or "@alice:example.com"
    endpoint.responses[("GET", f"/_matrix/client/v3/profile/{target}")] = (
        {"displayname": "Display name"},
        200,
    )
    profile = await MatrixAPIClient(driver=driver).get_profile(user_id)
    assert profile.model_dump() == {
        "user_id": target,
        "displayname": "Display name",
        "avatar_url": None,
    }


@pytest.mark.parametrize(
    ("method", "field_name", "value"),
    [
        ("set_display_name", "displayname", "New name"),
        ("set_display_name", "displayname", ""),
        ("set_avatar", "avatar_url", "mxc://example.com:8448/avatar"),
        ("set_avatar", "avatar_url", ""),
    ],
)
async def test_profile_writes_use_only_configured_identity(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], method: str, field_name: str, value: str
) -> None:
    driver, endpoint = matrix
    path = f"{PROFILE_PATH}/{field_name}"
    endpoint.responses[("PUT", path)] = ({}, 200)
    assert await getattr(MatrixAPIClient(driver=driver), method)(value) is None
    assert endpoint.requests[0]["path"] == path
    assert endpoint.requests[0]["body"] == {field_name: value}


@pytest.mark.parametrize(
    "avatar_url",
    [
        "https://example.com/avatar.png",
        "file:///avatar.png",
        "mxc://example.com",
        "mxc:///media",
        "mxc://example.com/",
        "mxc://example.com/media/extra",
        "mxc://example.com/media?query=1",
        "mxc://example.com/media#fragment",
        "mxc://user@example.com/media",
        "mxc://example.com:bad/media",
        "mxc://example.com:65536/media",
        "mxc://example..com/media",
        "mxc://./media",
        "mxc://example.com/media%zz",
        "mxc://example.com/avatar.png",
        "mxc://example.com/media%20id",
        "mxc://example.com/media id",
        "mxc://example.com/media\n",
    ],
)
@pytest.mark.parametrize("method", ["set_avatar", "set_room_avatar"])
async def test_avatar_writes_reject_non_mxc_and_malformed_uris_before_http(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], method: str, avatar_url: str
) -> None:
    driver, endpoint = matrix
    args = (ROOM, avatar_url) if method == "set_room_avatar" else (avatar_url,)
    with pytest.raises(ValueError, match=r"[Mm][Xx][Cc]"):
        await getattr(driver, method)(*args)
    assert endpoint.requests == []


@pytest.mark.parametrize(
    ("method", "args", "http_method", "path"),
    [
        ("list_room_members", (ROOM,), "GET", f"{ROOM_PATH}/joined_members"),
        ("invite_user", (ROOM, "@bob:example.com"), "POST", f"{ROOM_PATH}/invite"),
        ("set_room_name", (ROOM, "Name"), "PUT", f"{ROOM_PATH}/state/m.room.name"),
        ("set_room_topic", (ROOM, "Topic"), "PUT", f"{ROOM_PATH}/state/m.room.topic"),
        ("set_room_avatar", (ROOM, ""), "PUT", f"{ROOM_PATH}/state/m.room.avatar"),
        ("get_profile", (), "GET", PROFILE_PATH),
        ("set_display_name", ("Name",), "PUT", f"{PROFILE_PATH}/displayname"),
        ("set_avatar", ("",), "PUT", f"{PROFILE_PATH}/avatar_url"),
        ("search_users", ("Alice",), "POST", SEARCH_PATH),
    ],
)
async def test_permission_failures_propagate(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint],
    method: str,
    args: tuple[str, ...],
    http_method: str,
    path: str,
) -> None:
    driver, endpoint = matrix
    endpoint.responses[(http_method, path)] = (
        {"errcode": "M_FORBIDDEN", "error": "Request denied"},
        403,
    )
    with pytest.raises(RuntimeError, match="M_FORBIDDEN"):
        await getattr(driver, method)(*args)


async def test_user_search_preserves_profiles_and_sends_auth_and_custom_headers(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint],
) -> None:
    driver, endpoint = matrix
    endpoint.responses[("POST", SEARCH_PATH)] = (
        {
            "results": [
                {"user_id": "@alice:example.com", "display_name": "Alice"},
                {"user_id": "@bob:example.com", "avatar_url": "mxc://example.com/bob"},
            ],
            "limited": True,
        },
        200,
    )
    result = await MatrixAPIClient(driver=driver).search_users("example", limit=2)
    assert result.model_dump() == {
        "results": [
            {"user_id": "@alice:example.com", "displayname": "Alice", "avatar_url": None},
            {
                "user_id": "@bob:example.com",
                "displayname": None,
                "avatar_url": "mxc://example.com/bob",
            },
        ],
        "limited": True,
    }
    request = endpoint.requests[0]
    assert request["body"] == {"search_term": "example", "limit": 2}
    assert request["headers"]["Authorization"] == "Bearer test-token"
    assert request["headers"]["X-Example"] == "custom-value"
    assert request["headers"]["Content-Type"] == "application/json"


async def test_user_search_enforces_requested_result_bound(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint],
) -> None:
    driver, endpoint = matrix
    endpoint.responses[("POST", SEARCH_PATH)] = (
        {
            "results": [{"user_id": "@alice:example.com"}, {"user_id": "@bob:example.com"}],
            "limited": False,
        },
        200,
    )
    result = await driver.search_users("example", limit=1)
    assert [profile.user_id for profile in result.results] == ["@alice:example.com"]
    assert result.limited is True


@pytest.mark.parametrize(
    ("search_term", "limit"), [("", 1), ("  \n", 1), ("alice", 0), ("alice", 101)]
)
async def test_user_search_rejects_invalid_bounds_and_empty_query_before_http(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], search_term: str, limit: int
) -> None:
    driver, endpoint = matrix
    with pytest.raises(ValueError, match=r"limit|search"):
        await driver.search_users(search_term, limit=limit)
    assert endpoint.requests == []


@pytest.mark.parametrize(
    "data", [{}, {"results": [], "limited": "false"}, {"results": [{}], "limited": False}]
)
async def test_user_search_rejects_malformed_response(
    matrix: tuple[NioMatrixDriver, MatrixEndpoint], data: object
) -> None:
    driver, endpoint = matrix
    endpoint.responses[("POST", SEARCH_PATH)] = (data, 200)
    with pytest.raises(RuntimeError, match="search"):
        await driver.search_users("alice")
