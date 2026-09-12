from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.matrix_http import MatrixHTTP
from matrix_mcp.matrix_rooms import MatrixRooms

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ROOM = "!room:example.com"
ROOM_PATH = f"/_matrix/client/v3/rooms/{ROOM}"


@dataclass
class RoomEndpoint:
    responses: dict[tuple[str, str], tuple[object, int]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": await request.json() if request.can_read_body else None,
            }
        )
        data, status = self.responses.get(
            (request.method, request.path), ({"errcode": "M_NOT_FOUND"}, 404)
        )
        return web.json_response(data, status=status)


@pytest.fixture
async def matrix() -> AsyncIterator[tuple[MatrixRooms, RoomEndpoint]]:
    endpoint = RoomEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        http = MatrixHTTP(
            MatrixMCPConfig(
                homeserver=str(server.make_url("/")),
                user_id="@alice:example.com",
                device_id="TESTDEVICE",
                access_token="test-token",
                http_headers={"X-Example": "custom-value"},
            )
        )
        yield MatrixRooms(http), endpoint


async def test_create_room_is_private(matrix: tuple[MatrixRooms, RoomEndpoint]) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("POST", "/_matrix/client/v3/createRoom")] = (
        {"room_id": "!created:example.com"},
        200,
    )
    assert await rooms.create(name="Planning", invite=["@bob:example.com"]) == (
        "!created:example.com"
    )
    request = endpoint.requests[-1]
    assert request["body"] == {
        "visibility": "private",
        "preset": "private_chat",
        "name": "Planning",
        "invite": ["@bob:example.com"],
    }
    assert request["headers"]["Authorization"] == "Bearer test-token"
    assert request["headers"]["X-Example"] == "custom-value"


@pytest.mark.parametrize("identifier", ["!room:example.com", "#project:example.com", "!room_hash"])
async def test_join_resolves_room_ids_and_aliases(
    matrix: tuple[MatrixRooms, RoomEndpoint], identifier: str
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("POST", f"/_matrix/client/v3/join/{identifier}")] = (
        {"room_id": ROOM},
        200,
    )
    assert await rooms.join(identifier) == ROOM
    assert endpoint.requests[-1]["method"] == "POST"


async def test_leave_passes_explicit_reason(matrix: tuple[MatrixRooms, RoomEndpoint]) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("POST", f"{ROOM_PATH}/leave")] = ({}, 200)
    await rooms.leave(ROOM, reason="No longer needed")
    assert endpoint.requests[-1]["body"] == {"reason": "No longer needed"}


@pytest.mark.parametrize("public_receipt", [False, True])
async def test_mark_read_defaults_to_private_receipt(
    matrix: tuple[MatrixRooms, RoomEndpoint], *, public_receipt: bool
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$last")] = (
        {
            "event_id": "$last",
            "sender": "@bob:example.com",
            "type": "m.room.message",
            "content": {"body": "hello", "msgtype": "m.text"},
        },
        200,
    )
    endpoint.responses[("POST", f"{ROOM_PATH}/read_markers")] = ({}, 200)
    marked_unread_path = (
        f"/_matrix/client/v3/user/@alice:example.com/rooms/{ROOM}/account_data/m.marked_unread"
    )
    endpoint.responses[("PUT", marked_unread_path)] = ({}, 200)
    if public_receipt:
        await rooms.mark_read(ROOM, "$last", public_receipt=True)
    else:
        await rooms.mark_read(ROOM, "$last")
    receipt = "m.read" if public_receipt else "m.read.private"
    assert endpoint.requests[-2]["body"] == {"m.fully_read": "$last", receipt: "$last"}
    clear_request = endpoint.requests[-1]
    assert clear_request["method"] == "PUT"
    assert clear_request["path"] == marked_unread_path
    assert clear_request["body"] == {"unread": False}


async def test_denied_event_never_updates_read_state(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$last")] = (
        {"errcode": "M_FORBIDDEN"},
        403,
    )
    with pytest.raises(RuntimeError, match="M_FORBIDDEN"):
        await rooms.mark_read(ROOM, "$last")
    assert [request["method"] for request in endpoint.requests] == ["GET"]


@pytest.mark.parametrize("identifier", ["room", "https://example.com", "!room:example.com/leave"])
async def test_invalid_join_target_is_rejected_before_http(
    matrix: tuple[MatrixRooms, RoomEndpoint], identifier: str
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match="Invalid Matrix"):
        await rooms.join(identifier)
    assert not endpoint.requests


async def test_invalid_invitee_does_not_create_room(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match="Invalid Matrix"):
        await rooms.create(invite=["bob"])
    assert not endpoint.requests


async def test_invitations_include_inviter_and_stable_pages(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", "/_matrix/client/v3/sync")] = (
        {
            "next_batch": "next",
            "rooms": {
                "invite": {
                    "!z:example.com": {"invite_state": {"events": []}},
                    "!a:example.com": {
                        "invite_state": {
                            "events": [
                                {"type": "m.room.name", "content": {"name": "Planning"}},
                                {
                                    "type": "m.room.member",
                                    "state_key": "@alice:example.com",
                                    "sender": "@bob:example.com",
                                    "content": {"membership": "invite"},
                                },
                            ]
                        }
                    },
                }
            },
        },
        200,
    )
    first = await rooms.invitations(limit=1)
    second = await rooms.invitations(limit=1, offset=1)
    assert first.rooms[0].room_id == "!a:example.com"
    assert first.rooms[0].name == "Planning"
    assert first.rooms[0].inviter == "@bob:example.com"
    assert first.total == 2
    assert first.next_offset == 1
    assert second.rooms[0].room_id == "!z:example.com"
    assert second.next_offset is None
    assert {request["method"] for request in endpoint.requests} == {"GET"}
    sync_filter = json.loads(endpoint.requests[0]["query"]["filter"])
    assert sync_filter["event_fields"] == [
        "event_id",
        "sender",
        "origin_server_ts",
        "state_key",
        "type",
        "content.body",
        "content.m\\.mentions",
        "content.name",
        "content.unread",
    ]
    assert "m.room.member" in sync_filter["room"]["state"]["types"]
    assert sync_filter["room"]["timeline"]["types"] == []
    assert sync_filter["room"]["timeline"]["limit"] > 0
    assert endpoint.requests[0]["query"]["set_presence"] == "offline"


async def test_unread_returns_counts_mentions_and_server_cursors_without_marking(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    mention = {
        "event_id": "$mention",
        "sender": "@bob:example.com",
        "origin_server_ts": 123,
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": "Can you help?",
            "m.mentions": {"user_ids": ["@alice:example.com"]},
        },
    }
    endpoint.responses[("GET", "/_matrix/client/v3/sync")] = (
        {
            "next_batch": "next",
            "rooms": {
                "join": {
                    ROOM: {
                        "state": {
                            "events": [{"type": "m.room.name", "content": {"name": "Planning"}}]
                        },
                        "timeline": {"events": [mention], "limited": True, "prev_batch": "older"},
                        "unread_notifications": {"notification_count": 3, "highlight_count": 1},
                    },
                    "!read:example.com": {"timeline": {"events": [mention]}},
                }
            },
        },
        200,
    )
    page = await rooms.unread(timeline_limit=7)
    assert set(page.model_dump()) == {"rooms", "total", "next_offset"}
    assert page.total == 1
    room = page.rooms[0]
    assert room.name == "Planning"
    assert room.notification_count == 3
    assert room.highlight_count == 1
    assert [event.event_id for event in room.mentions] == ["$mention"]
    assert room.limited is True
    assert room.prev_batch == "older"
    request = endpoint.requests[0]
    assert "since" not in request["query"]
    sync_filter = json.loads(request["query"]["filter"])
    assert sync_filter["event_fields"] == [
        "event_id",
        "sender",
        "origin_server_ts",
        "state_key",
        "type",
        "content.body",
        "content.m\\.mentions",
        "content.name",
        "content.unread",
    ]
    assert sync_filter["room"]["timeline"]["limit"] == 7
    assert sync_filter["room"]["timeline"]["types"] == ["m.room.message"]
    assert request["query"]["set_presence"] == "offline"
    assert [request["method"] for request in endpoint.requests] == ["GET"]


@pytest.mark.parametrize("method", ["invitations", "unread"])
@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_sync_page_bounds_reject_before_http(
    matrix: tuple[MatrixRooms, RoomEndpoint], method: str, arguments: dict[str, int]
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match="limit"):
        await getattr(rooms, method)(**arguments)
    assert not endpoint.requests


async def test_malformed_sync_is_an_error(matrix: tuple[MatrixRooms, RoomEndpoint]) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", "/_matrix/client/v3/sync")] = ({"rooms": []}, 200)
    with pytest.raises(RuntimeError, match=r"invalid|Malformed"):
        await rooms.unread()


async def test_too_many_invitees_does_not_create_room(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match="100 invitees"):
        await rooms.create(invite=[f"@user{number}:example.com" for number in range(101)])
    assert not endpoint.requests
