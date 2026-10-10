from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.e2ee import DecryptedEvent
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


async def test_mark_read_reports_partial_success_when_unread_clear_fails(
    matrix: tuple[MatrixRooms, RoomEndpoint],
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
    endpoint.responses[("PUT", marked_unread_path)] = (
        {"errcode": "M_UNKNOWN", "error": "do-not-echo-this"},
        500,
    )

    with pytest.raises(RuntimeError, match=r"read markers were updated.*safe to retry") as error:
        await rooms.mark_read(ROOM, "$last")

    assert "do-not-echo-this" not in str(error.value)
    assert [request["method"] for request in endpoint.requests] == ["GET", "POST", "PUT"]


async def test_mark_read_does_not_clear_unread_after_marker_failure(
    matrix: tuple[MatrixRooms, RoomEndpoint],
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
    endpoint.responses[("POST", f"{ROOM_PATH}/read_markers")] = (
        {"errcode": "M_FORBIDDEN"},
        403,
    )

    with pytest.raises(RuntimeError, match="M_FORBIDDEN"):
        await rooms.mark_read(ROOM, "$last")

    assert [request["method"] for request in endpoint.requests] == ["GET", "POST"]


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
        "content.algorithm",
        "content.ciphertext",
        "content.sender_key",
        "content.session_id",
        "content.device_id",
    ]
    assert sync_filter["room"]["timeline"]["limit"] == 7
    assert sync_filter["room"]["timeline"]["types"] == ["m.room.message", "m.room.encrypted"]
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


class MentionCrypto:
    def __init__(self) -> None:
        self.decrypted: list[str] = []

    async def decrypt(self, room_id: str, raw: dict[str, Any]) -> DecryptedEvent:
        assert room_id == ROOM
        self.decrypted.append(raw["event_id"])
        content = {
            "msgtype": "m.text",
            "body": "Encrypted question",
            "m.mentions": {"user_ids": ["@alice:example.com"]},
        }
        return DecryptedEvent({**raw, "type": "m.room.message", "content": content})

    async def encrypt(
        self, room_id: str, event_type: str, content: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        del room_id, event_type, content
        raise AssertionError


@pytest.mark.parametrize("mode", ["local", "hosted"])
async def test_unread_reads_mentions_inside_encrypted_messages(
    matrix: tuple[MatrixRooms, RoomEndpoint], mode: str
) -> None:
    rooms, endpoint = matrix
    crypto = MentionCrypto()
    if mode == "local":
        rooms.crypto = crypto
    sealed = {
        "event_id": "$sealed",
        "sender": "@bob:example.com",
        "origin_server_ts": 123,
        "type": "m.room.encrypted",
        "content": {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque"},
    }
    endpoint.responses[("GET", "/_matrix/client/v3/sync")] = (
        {
            "next_batch": "next",
            "rooms": {
                "join": {
                    ROOM: {
                        "timeline": {"events": [sealed]},
                        "unread_notifications": {"notification_count": 1},
                    },
                    "!read:example.com": {"timeline": {"events": [sealed]}},
                }
            },
        },
        200,
    )

    page = await rooms.unread()

    mentions = page.rooms[0].mentions
    if mode == "local":
        assert [(event.event_id, event.body) for event in mentions] == [
            ("$sealed", "Encrypted question")
        ]
        assert crypto.decrypted == ["$sealed"]
    else:
        assert mentions == []


DIRECT_PATH = "/_matrix/client/v3/user/@alice:example.com/account_data/m.direct"
JOINED_PATH = "/_matrix/client/v3/joined_rooms"
HIERARCHY_PATH = f"/_matrix/client/v1/rooms/{ROOM}/hierarchy"
SYNC_PATH = "/_matrix/client/v3/sync"


def receipt_sync(*events: dict[str, Any], room_id: str = ROOM) -> dict[str, Any]:
    return {
        "next_batch": "next",
        "rooms": {"join": {room_id: {"ephemeral": {"events": list(events)}}}},
    }


def receipt_event(content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "m.receipt", "content": content}


def member_path(room_id: str, user_id: str) -> str:
    return f"/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_id}"


def members(*memberships: tuple[str, str]) -> dict[str, Any]:
    return {
        "chunk": [
            {"type": "m.room.member", "state_key": user, "content": {"membership": membership}}
            for user, membership in memberships
        ]
    }


async def test_create_dm_reuses_the_latest_two_person_chat(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    me, bob = "@alice:example.com", "@bob:example.com"
    listed = [
        "!old:example.com",
        "!invited:example.com",
        "!crowd:example.com",
        "!bob-left:example.com",
    ]
    endpoint.responses[("GET", DIRECT_PATH)] = ({bob: [*listed, "!i-left:example.com"]}, 200)
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": listed}, 200)
    for room_id, chunk in [
        ("!bob-left:example.com", members((me, "join"), (bob, "leave"))),
        (
            "!crowd:example.com",
            members((me, "join"), (bob, "join"), ("@eve:example.com", "invite")),
        ),
        ("!invited:example.com", members((me, "join"), (bob, "invite"))),
    ]:
        endpoint.responses[("GET", f"/_matrix/client/v3/rooms/{room_id}/members")] = (chunk, 200)

    result = await rooms.create_dm(bob)

    # A room with a third person in it is not a private chat with Bob.
    assert result.model_dump() == {"room_id": "!invited:example.com", "created": False}
    assert endpoint.requests[-2]["query"] == {"not_membership": "leave"}
    assert endpoint.requests[-1]["path"].endswith("/state/m.room.encryption")


@pytest.mark.parametrize(
    ("encrypted_room", "request_encrypted", "has_crypto", "reused"),
    [
        (False, False, False, True),
        (False, True, True, False),
        (True, True, True, True),
        (True, False, True, True),
        (True, False, False, False),
    ],
)
async def test_create_dm_reuses_only_rooms_this_mode_can_use(
    matrix: tuple[MatrixRooms, RoomEndpoint],
    *,
    encrypted_room: bool,
    request_encrypted: bool,
    has_crypto: bool,
    reused: bool,
) -> None:
    rooms, endpoint = matrix
    rooms.crypto = MentionCrypto() if has_crypto else None
    endpoint.responses[("GET", DIRECT_PATH)] = ({"@bob:example.com": ["!dm:example.com"]}, 200)
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": ["!dm:example.com"]}, 200)
    endpoint.responses[("GET", "/_matrix/client/v3/rooms/!dm:example.com/members")] = (
        members(("@alice:example.com", "join"), ("@bob:example.com", "join")),
        200,
    )
    if encrypted_room:
        endpoint.responses[
            ("GET", "/_matrix/client/v3/rooms/!dm:example.com/state/m.room.encryption")
        ] = ({"algorithm": "m.megolm.v1.aes-sha2"}, 200)
    endpoint.responses[("POST", "/_matrix/client/v3/createRoom")] = (
        {"room_id": "!new:example.com"},
        200,
    )
    endpoint.responses[("PUT", DIRECT_PATH)] = ({}, 200)

    result = await rooms.create_dm("@bob:example.com", encrypted=request_encrypted)

    assert result.created is not reused
    assert result.room_id == ("!dm:example.com" if reused else "!new:example.com")


async def test_create_dm_creates_a_trusted_private_room_and_records_it(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", DIRECT_PATH)] = (
        {
            "@carol:example.com": ["!carol:example.com"],
            "@bob:example.com": ["!left:example.com"],
            "@dave:example.com": "not-a-list",
        },
        200,
    )
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": ["!carol:example.com"]}, 200)
    endpoint.responses[("POST", "/_matrix/client/v3/createRoom")] = (
        {"room_id": "!dm:example.com"},
        200,
    )
    endpoint.responses[("PUT", DIRECT_PATH)] = ({}, 200)

    result = await rooms.create_dm("@bob:example.com")

    assert result.model_dump() == {"room_id": "!dm:example.com", "created": True}
    create, reread, record = endpoint.requests[-3:]
    # The map is read again after creating the room, so concurrent changes survive.
    assert (reread["method"], reread["path"]) == ("GET", DIRECT_PATH)
    assert create["body"] == {
        "visibility": "private",
        "preset": "trusted_private_chat",
        "is_direct": True,
        "invite": ["@bob:example.com"],
    }
    assert record["method"] == "PUT"
    assert record["path"] == DIRECT_PATH
    assert record["body"] == {
        "@carol:example.com": ["!carol:example.com"],
        "@bob:example.com": ["!left:example.com", "!dm:example.com"],
        "@dave:example.com": "not-a-list",
    }


async def test_create_dm_starts_the_direct_marker_when_none_exists(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": []}, 200)
    endpoint.responses[("POST", "/_matrix/client/v3/createRoom")] = (
        {"room_id": "!dm:example.com"},
        200,
    )
    endpoint.responses[("PUT", DIRECT_PATH)] = ({}, 200)

    assert (await rooms.create_dm("@bob:example.com")).created is True
    assert endpoint.requests[-1]["body"] == {"@bob:example.com": ["!dm:example.com"]}


async def test_encrypted_dm_needs_local_mode_before_any_request(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match="local"):
        await rooms.create_dm("@bob:example.com", encrypted=True)
    assert not endpoint.requests


async def test_encrypted_dm_enables_megolm_from_the_start(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    rooms.crypto = MentionCrypto()
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": []}, 200)
    endpoint.responses[("POST", "/_matrix/client/v3/createRoom")] = (
        {"room_id": "!dm:example.com"},
        200,
    )
    endpoint.responses[("PUT", DIRECT_PATH)] = ({}, 200)

    await rooms.create_dm("@bob:example.com", encrypted=True)

    create = next(request for request in endpoint.requests if request["method"] == "POST")
    assert create["body"]["initial_state"] == [
        {
            "type": "m.room.encryption",
            "state_key": "",
            "content": {"algorithm": "m.megolm.v1.aes-sha2"},
        }
    ]


async def test_dm_marker_failure_names_the_created_room(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": []}, 200)
    endpoint.responses[("POST", "/_matrix/client/v3/createRoom")] = (
        {"room_id": "!dm:example.com"},
        200,
    )
    endpoint.responses[("PUT", DIRECT_PATH)] = (
        {"errcode": "M_UNKNOWN", "error": "do-not-echo-this"},
        500,
    )

    with pytest.raises(RuntimeError, match=r"!dm:example\.com.*not saved") as error:
        await rooms.create_dm("@bob:example.com")

    assert "do-not-echo-this" not in str(error.value)


@pytest.mark.parametrize("user_id", ["@alice:example.com", "bob", "@bob"])
async def test_invalid_dm_targets_are_rejected_before_http(
    matrix: tuple[MatrixRooms, RoomEndpoint], user_id: str
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match=r"connected user|Invalid Matrix"):
        await rooms.create_dm(user_id)
    assert not endpoint.requests


async def test_hierarchy_lists_rooms_children_and_joined_state(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", HIERARCHY_PATH)] = (
        {
            "rooms": [
                {
                    "room_id": ROOM,
                    "name": "Team",
                    "topic": "Everything",
                    "canonical_alias": "#team:example.com",
                    "room_type": "m.space",
                    "num_joined_members": 12,
                    "join_rule": "invite",
                    "world_readable": False,
                    "guest_can_join": False,
                    "children_state": [
                        {
                            "type": "m.space.child",
                            "state_key": "!dev:example.com",
                            "sender": "@bob:example.com",
                            "origin_server_ts": 1,
                            "content": {"via": ["example.com"]},
                        },
                        {
                            "type": "m.space.child",
                            "state_key": "!removed:example.com",
                            "content": {"via": []},
                        },
                        {
                            "type": "m.space.child",
                            "state_key": "!stale:example.com",
                            "content": {},
                        },
                    ],
                },
                {"room_id": "!dev:example.com", "num_joined_members": 3},
            ],
            "next_batch": "cursor-2",
        },
        200,
    )
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": [ROOM]}, 200)

    hierarchy = await rooms.hierarchy(ROOM, limit=10, max_depth=2, next_batch="cursor-1")

    assert hierarchy.next_batch == "cursor-2"
    space, child = hierarchy.rooms
    assert space.model_dump() == {
        "room_id": ROOM,
        "name": "Team",
        "topic": "Everything",
        "canonical_alias": "#team:example.com",
        "room_type": "m.space",
        "joined_member_count": 12,
        "join_rule": "invite",
        "joined": True,
        "children": ["!dev:example.com"],
    }
    assert child.room_id == "!dev:example.com"
    assert child.joined is False
    assert child.joined_member_count == 3
    assert child.children == []
    assert endpoint.requests[0]["query"] == {"limit": "10", "max_depth": "2", "from": "cursor-1"}


async def test_hierarchy_skips_malformed_rooms_instead_of_failing(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", HIERARCHY_PATH)] = (
        {
            "rooms": [
                {"room_id": ROOM, "name": "Team", "room_type": "m.space"},
                {"room_id": "!remote:other.example", "name": 7},
            ]
        },
        200,
    )
    endpoint.responses[("GET", JOINED_PATH)] = ({"joined_rooms": [ROOM]}, 200)

    result = await rooms.hierarchy(ROOM)

    assert [room.room_id for room in result.rooms] == [ROOM]
    assert result.skipped == 1


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 101}, {"max_depth": 0}, {"max_depth": 6}, {"next_batch": ""}],
)
async def test_hierarchy_bounds_reject_before_http(
    matrix: tuple[MatrixRooms, RoomEndpoint], arguments: dict[str, Any]
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match=r"limit|max_depth|cursor"):
        await rooms.hierarchy(ROOM, **arguments)
    assert not endpoint.requests


async def test_malformed_hierarchy_is_an_error(matrix: tuple[MatrixRooms, RoomEndpoint]) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", HIERARCHY_PATH)] = ({"rooms": {"room_id": ROOM}}, 200)
    with pytest.raises(RuntimeError, match="invalid space hierarchy"):
        await rooms.hierarchy(ROOM)


async def test_receipts_keep_each_readers_newest_receipt(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = (
        receipt_sync(
            receipt_event(
                {
                    "$old": {
                        "m.read": {
                            "@bob:example.com": {"ts": 100},
                            "@carol:example.com": {"ts": 150, "thread_id": "$root"},
                        }
                    },
                    "$new": {
                        "m.read": {"@bob:example.com": {"ts": 300}},
                        "m.read.private": {
                            "@alice:example.com": {"ts": 400},
                            "@mallory:example.com": {"ts": 999},
                        },
                    },
                }
            ),
            receipt_event({"$main": {"m.read": {"@carol:example.com": {"ts": 200}}}}),
            {"type": "m.typing", "content": {"user_ids": ["@bob:example.com"]}},
        ),
        200,
    )

    result = await rooms.receipts(ROOM, limit=3)

    assert result.total == 4
    assert [
        (receipt.user_id, receipt.event_id, receipt.timestamp_ms, receipt.thread_id)
        for receipt in result.receipts
    ] == [
        ("@alice:example.com", "$new", 400, None),
        ("@bob:example.com", "$new", 300, None),
        ("@carol:example.com", "$main", 200, None),
    ]
    assert [receipt.private for receipt in result.receipts] == [True, False, False]
    assert all(receipt.read is None for receipt in result.receipts)
    request = endpoint.requests[0]
    assert request["query"]["timeout"] == "0"
    assert request["query"]["set_presence"] == "offline"
    sync_filter = json.loads(request["query"]["filter"])
    assert sync_filter.pop("presence")["types"] == []
    assert sync_filter == {
        "account_data": {"types": []},
        "room": {
            "rooms": [ROOM],
            "state": {"types": []},
            "timeline": {"limit": 1},
            "ephemeral": {"types": ["m.receipt"]},
            "account_data": {"types": []},
        },
    }


async def test_receipts_for_a_room_missing_from_sync_are_empty(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = ({"next_batch": "next"}, 200)
    result = await rooms.receipts(ROOM)
    assert result.model_dump() == {"receipts": [], "total": 0}


def stored_event(event_id: str, timestamp: int) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": "@bob:example.com",
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": {"msgtype": "m.text", "body": "hello"},
    }


async def test_receipts_compare_server_timestamps_with_the_target_event(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = (
        receipt_sync(
            receipt_event(
                {
                    "$target": {"m.read": {"@bob:example.com": {"ts": 10}}},
                    "$later": {"m.read": {"@carol:example.com": {"ts": 9}}},
                    "$earlier": {"m.read": {"@dave:example.com": {"ts": 8}}},
                    "$hidden": {"m.read": {"@erin:example.com": {"ts": 7}}},
                }
            )
        ),
        200,
    )
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$target")] = (stored_event("$target", 200), 200)
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$later")] = (stored_event("$later", 300), 200)
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$earlier")] = (
        stored_event("$earlier", 100),
        200,
    )
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$hidden")] = ({"errcode": "M_FORBIDDEN"}, 403)

    result = await rooms.receipts(ROOM, event_id="$target")

    assert {receipt.user_id: receipt.read for receipt in result.receipts} == {
        "@bob:example.com": True,
        "@carol:example.com": True,
        "@dave:example.com": False,
        "@erin:example.com": None,
    }
    fetched = [request["path"] for request in endpoint.requests if "/event/" in request["path"]]
    assert fetched[0] == f"{ROOM_PATH}/event/$target"
    assert f"{ROOM_PATH}/event/$target" not in fetched[1:]


async def test_receipt_read_checks_stop_after_twenty_event_fetches(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    content = {
        f"$e{number}": {"m.read": {f"@user{number}:example.com": {"ts": 1000 - number}}}
        for number in range(25)
    }
    endpoint.responses[("GET", SYNC_PATH)] = (receipt_sync(receipt_event(content)), 200)
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$target")] = (stored_event("$target", 50), 200)
    for number in range(25):
        event_id = f"$e{number}"
        endpoint.responses[("GET", f"{ROOM_PATH}/event/{event_id}")] = (
            stored_event(event_id, 100),
            200,
        )

    result = await rooms.receipts(ROOM, event_id="$target", limit=100)

    assert [receipt.read for receipt in result.receipts] == [True] * 20 + [None] * 5
    event_fetches = [request for request in endpoint.requests if "/event/" in request["path"]]
    assert len(event_fetches) == 21


async def test_threaded_receipts_only_answer_for_their_own_thread(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = (
        receipt_sync(
            receipt_event(
                {
                    "$main-later": {
                        "m.read": {
                            "@bob:example.com": {"ts": 10, "thread_id": "main"},
                            "@carol:example.com": {"ts": 9},
                        }
                    },
                    "$thread-later": {
                        "m.read": {"@dave:example.com": {"ts": 8, "thread_id": "$root"}}
                    },
                }
            )
        ),
        200,
    )
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$target")] = (stored_event("$target", 100), 200)
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$main-later")] = (
        stored_event("$main-later", 200),
        200,
    )

    result = await rooms.receipts(ROOM, event_id="$target")

    # A receipt in another thread says nothing about the main timeline.
    assert {receipt.user_id: receipt.read for receipt in result.receipts} == {
        "@bob:example.com": True,
        "@carol:example.com": True,
        "@dave:example.com": None,
    }
    fetched = [request["path"] for request in endpoint.requests if "/event/" in request["path"]]
    assert f"{ROOM_PATH}/event/$thread-later" not in fetched


async def test_receipts_skip_malformed_event_and_user_keys(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = (
        receipt_sync(
            receipt_event(
                {
                    "not-an-event": {"m.read": {"@bob:example.com": {"ts": 10}}},
                    "$ok": {"m.read": {"bob": {"ts": 9}, "@carol:example.com": {"ts": 8}}},
                }
            )
        ),
        200,
    )
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$ok")] = (stored_event("$ok", 100), 200)

    result = await rooms.receipts(ROOM, event_id="$ok")

    assert [(receipt.user_id, receipt.event_id, receipt.read) for receipt in result.receipts] == [
        ("@carol:example.com", "$ok", True)
    ]


async def test_receipt_read_checks_do_not_hide_server_errors(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = (
        receipt_sync(receipt_event({"$other": {"m.read": {"@bob:example.com": {"ts": 1}}}})),
        200,
    )
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$target")] = (stored_event("$target", 50), 200)
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$other")] = ({"errcode": "M_UNKNOWN"}, 500)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await rooms.receipts(ROOM, event_id="$target")


@pytest.mark.parametrize(
    "arguments", [{"limit": 0}, {"limit": 101}, {"event_id": "event"}, {"event_id": "$"}]
)
async def test_receipt_arguments_reject_before_http(
    matrix: tuple[MatrixRooms, RoomEndpoint], arguments: dict[str, Any]
) -> None:
    rooms, endpoint = matrix
    with pytest.raises(ValueError, match=r"limit|event ID"):
        await rooms.receipts(ROOM, **arguments)
    assert not endpoint.requests


async def test_latest_activity_maps_rooms_to_their_newest_message(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = (
        {
            "next_batch": "next",
            "rooms": {
                "join": {
                    ROOM: {
                        "timeline": {
                            "events": [
                                {"type": "m.room.message", "origin_server_ts": 500},
                                {"type": "m.room.encrypted", "origin_server_ts": 700},
                            ]
                        }
                    },
                    "!quiet:example.com": {"timeline": {"events": []}},
                    "!odd:example.com": {
                        "timeline": {
                            "events": [{"type": "m.room.message", "origin_server_ts": True}]
                        }
                    },
                }
            },
        },
        200,
    )

    assert await rooms.latest_activity() == {ROOM: 700}
    sync_filter = json.loads(endpoint.requests[0]["query"]["filter"])
    assert sync_filter["event_fields"] == ["origin_server_ts", "type"]
    assert sync_filter["room"]["timeline"] == {
        "limit": 10,
        "types": ["m.room.message", "m.room.encrypted"],
    }
    assert sync_filter["room"]["state"] == {"types": []}
    assert sync_filter["room"]["ephemeral"] == {"types": []}
    assert sync_filter["presence"]["types"] == []


async def test_snapshots_never_repeat_a_sync_request(
    matrix: tuple[MatrixRooms, RoomEndpoint],
) -> None:
    rooms, endpoint = matrix
    endpoint.responses[("GET", SYNC_PATH)] = ({"next_batch": "next"}, 200)

    await rooms.unread()
    await rooms.unread()

    # Synapse answers an identical sync request from its response cache for minutes.
    first, second = (json.loads(request["query"]["filter"]) for request in endpoint.requests)
    assert first["presence"]["types"] == second["presence"]["types"] == []
    assert first["presence"]["not_types"] != second["presence"]["not_types"]
    first.pop("presence")
    second.pop("presence")
    assert first == second
