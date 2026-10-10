from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.matrix_http import MatrixHTTP, MatrixHTTPError
from matrix_mcp.matrix_moderation import MatrixModeration, PowerLevels

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ME = "@alice:example.com"
ROOM = "!room:example.com"
ROOM_PATH = f"/_matrix/client/v3/rooms/{ROOM}"
POWER_LEVELS_PATH = f"{ROOM_PATH}/state/m.room.power_levels"
PINNED_PATH = f"{ROOM_PATH}/state/m.room.pinned_events"


@dataclass
class RoomEndpoint:
    responses: dict[tuple[str, str], tuple[object, int]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "body": await request.json() if request.can_read_body else None,
            }
        )
        data, status = self.responses.get(
            (request.method, request.path), ({"errcode": "M_NOT_FOUND"}, 404)
        )
        return web.json_response(data, status=status)

    def writes(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["method"] != "GET"]


@pytest.fixture
async def matrix() -> AsyncIterator[tuple[MatrixModeration, RoomEndpoint]]:
    endpoint = RoomEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        http = MatrixHTTP(
            MatrixMCPConfig(
                homeserver=str(server.make_url("/")),
                user_id=ME,
                device_id="TESTDEVICE",
                access_token="test-token",
            )
        )
        yield MatrixModeration(http), endpoint


@pytest.mark.parametrize("action", ["kick", "ban", "unban"])
async def test_membership_actions_post_user_and_reason(
    matrix: tuple[MatrixModeration, RoomEndpoint], action: str
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("POST", f"{ROOM_PATH}/{action}")] = ({}, 200)
    await getattr(moderation, action)(ROOM, "@bob:example.com", reason="spam")
    await getattr(moderation, action)(ROOM, "@bob:example.com")
    assert [request["body"] for request in endpoint.requests] == [
        {"user_id": "@bob:example.com", "reason": "spam"},
        {"user_id": "@bob:example.com"},
    ]
    assert {request["path"] for request in endpoint.requests} == {f"{ROOM_PATH}/{action}"}


@pytest.mark.parametrize("action", ["kick", "ban"])
async def test_kick_and_ban_refuse_the_connected_user(
    matrix: tuple[MatrixModeration, RoomEndpoint], action: str
) -> None:
    moderation, endpoint = matrix
    with pytest.raises(ValueError, match="matrix_leave_room"):
        await getattr(moderation, action)(ROOM, ME)
    assert endpoint.requests == []


@pytest.mark.parametrize("user_id", ["bob", "@bob", "@bob:example.com/x", "@bo b:example.com"])
async def test_invalid_user_ids_reject_before_http(
    matrix: tuple[MatrixModeration, RoomEndpoint], user_id: str
) -> None:
    moderation, endpoint = matrix
    with pytest.raises(ValueError, match="user ID"):
        await moderation.kick(ROOM, user_id)
    with pytest.raises(ValueError, match="user ID"):
        await moderation.set_power_level(ROOM, user_id, 50)
    assert endpoint.requests == []


async def test_permission_errors_propagate(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("POST", f"{ROOM_PATH}/ban")] = ({"errcode": "M_FORBIDDEN"}, 403)
    with pytest.raises(MatrixHTTPError, match="M_FORBIDDEN"):
        await moderation.ban(ROOM, "@bob:example.com")


async def test_missing_power_levels_use_spec_defaults(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, _ = matrix
    # Without a power levels event, sending state needs level 0 instead of 50.
    assert await moderation.power_levels(ROOM) == PowerLevels(own_level=0, state_default=0)


@pytest.mark.parametrize(
    "create",
    [
        {"type": "m.room.create", "sender": ME, "content": {"room_version": "11"}},
        {"creator": ME, "room_version": "10"},
    ],
)
async def test_creator_has_level_100_when_power_levels_are_missing(
    matrix: tuple[MatrixModeration, RoomEndpoint], create: dict[str, Any]
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/state/m.room.create")] = (create, 200)
    levels = await moderation.power_levels(ROOM)
    assert levels.users == {ME: 100}
    assert levels.own_level == 100


async def test_power_levels_parse_leniently_and_report_own_level(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", POWER_LEVELS_PATH)] = (
        {
            "users": {ME: "100", "@bob:example.com": 50, "@eve:example.com": "high"},
            "users_default": "-1",
            "events": {"m.room.name": 75, "m.room.topic": None},
            "events_default": 10,
            "state_default": True,
            "kick": "+60",
            "ban": 70.5,
            "redact": 20,
        },
        200,
    )
    levels = await moderation.power_levels(ROOM)
    assert levels.model_dump() == {
        "creators": [],
        "users": {ME: 100, "@bob:example.com": 50},
        "users_default": -1,
        "events": {"m.room.name": 75},
        "events_default": 10,
        "state_default": 50,
        "invite": 0,
        "kick": 60,
        "ban": 50,
        "redact": 20,
        "own_level": 100,
    }


async def test_own_level_falls_back_to_users_default(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", POWER_LEVELS_PATH)] = ({"users_default": 5}, 200)
    assert (await moderation.power_levels(ROOM)).own_level == 5


V12_ROOM = "!createhash"
V12_PATH = f"/_matrix/client/v3/rooms/{V12_ROOM}"


async def test_room_version_12_creators_outrank_power_levels(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", f"{V12_PATH}/state/m.room.power_levels")] = (
        {"users": {"@bob:example.com": 50}},
        200,
    )
    endpoint.responses[("GET", f"{V12_PATH}/state/m.room.create")] = (
        {
            "type": "m.room.create",
            "sender": ME,
            "content": {"room_version": "12", "additional_creators": ["@carol:example.com"]},
        },
        200,
    )
    levels = await moderation.power_levels(V12_ROOM)
    assert levels.creators == [ME, "@carol:example.com"]
    assert levels.own_level is None
    with pytest.raises(ValueError, match="creators outrank"):
        await moderation.set_power_level(V12_ROOM, "@carol:example.com", 50)
    assert endpoint.writes() == []


async def test_creators_come_from_the_create_event_without_the_format_parameter(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", f"{V12_PATH}/state/m.room.create")] = ({"room_version": "12"}, 200)
    # Room version 12 derives the room ID from the create event ID.
    endpoint.responses[("GET", f"{V12_PATH}/event/$createhash")] = (
        {"type": "m.room.create", "sender": "@bob:example.com", "content": {}},
        200,
    )
    levels = await moderation.power_levels(V12_ROOM)
    assert levels.creators == ["@bob:example.com"]
    assert levels.own_level == 0


@pytest.mark.parametrize("room_version", ["11", "1", None])
async def test_older_room_versions_have_no_privileged_creators(
    matrix: tuple[MatrixModeration, RoomEndpoint], room_version: str | None
) -> None:
    moderation, endpoint = matrix
    content = {} if room_version is None else {"room_version": room_version}
    endpoint.responses[("GET", f"{ROOM_PATH}/state/m.room.create")] = (
        {"type": "m.room.create", "sender": ME, "content": content},
        200,
    )
    endpoint.responses[("GET", POWER_LEVELS_PATH)] = ({"users": {ME: 100}}, 200)
    levels = await moderation.power_levels(ROOM)
    assert levels.creators == []
    assert levels.own_level == 100


def power_levels_content() -> dict[str, Any]:
    return {
        "users": {ME: 100, "@bob:example.com": 50, "@carol:example.com": "25"},
        "users_default": 0,
        "events": {"m.room.name": 50},
        "ban": 50,
        "notifications": {"room": 50},
    }


@pytest.mark.parametrize(
    ("level", "expected_bob"),
    [(75, {"@bob:example.com": 75}), (None, {})],
)
async def test_set_power_level_updates_only_the_user_entry(
    matrix: tuple[MatrixModeration, RoomEndpoint],
    level: int | None,
    expected_bob: dict[str, int],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", POWER_LEVELS_PATH)] = (power_levels_content(), 200)
    endpoint.responses[("PUT", POWER_LEVELS_PATH)] = ({"event_id": "$levels"}, 200)

    assert await moderation.set_power_level(ROOM, "@bob:example.com", level) == "$levels"

    expected = power_levels_content()
    expected["users"] = {ME: 100, "@carol:example.com": "25", **expected_bob}
    [write] = endpoint.writes()
    assert write["body"] == expected


@pytest.mark.parametrize(
    ("current", "users_default", "level"),
    [(100, 0, 50), (100, 0, None), (50, 50, -1)],
)
async def test_set_power_level_refuses_self_demotion(
    matrix: tuple[MatrixModeration, RoomEndpoint],
    current: int,
    users_default: int,
    level: int | None,
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", POWER_LEVELS_PATH)] = (
        {"users": {ME: current}, "users_default": users_default},
        200,
    )
    with pytest.raises(ValueError, match="own power level"):
        await moderation.set_power_level(ROOM, ME, level)
    assert endpoint.writes() == []


@pytest.mark.parametrize("level", [50, 100])
async def test_set_power_level_allows_keeping_or_raising_own_level(
    matrix: tuple[MatrixModeration, RoomEndpoint], level: int
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", POWER_LEVELS_PATH)] = ({"users": {ME: 50}}, 200)
    endpoint.responses[("PUT", POWER_LEVELS_PATH)] = ({"event_id": "$levels"}, 200)
    assert await moderation.set_power_level(ROOM, ME, level) == "$levels"
    assert endpoint.writes()[0]["body"] == {"users": {ME: level}}


@pytest.mark.parametrize("level", [2**53, -(2**53), True])
async def test_set_power_level_rejects_out_of_range_levels(
    matrix: tuple[MatrixModeration, RoomEndpoint], level: int
) -> None:
    moderation, endpoint = matrix
    with pytest.raises(ValueError, match="level must be an integer"):
        await moderation.set_power_level(ROOM, "@bob:example.com", level)
    assert endpoint.requests == []


async def test_set_power_level_needs_an_existing_power_levels_event(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    with pytest.raises(RuntimeError, match="no power levels event"):
        await moderation.set_power_level(ROOM, "@bob:example.com", 50)
    assert endpoint.writes() == []


async def test_set_power_level_requires_an_event_id_response(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", POWER_LEVELS_PATH)] = ({"users": {}}, 200)
    endpoint.responses[("PUT", POWER_LEVELS_PATH)] = ({}, 200)
    with pytest.raises(RuntimeError, match="event ID"):
        await moderation.set_power_level(ROOM, "@bob:example.com", 50)


async def test_pin_checks_the_event_and_appends_it(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$new")] = ({"event_id": "$new"}, 200)
    endpoint.responses[("GET", PINNED_PATH)] = ({"pinned": ["$old", 7], "extra": 1}, 200)
    endpoint.responses[("PUT", PINNED_PATH)] = ({"event_id": "$pins"}, 200)

    result = await moderation.pin(ROOM, "$new")

    assert result.pinned == ["$old", "$new"]
    assert result.changed is True
    assert endpoint.requests[0]["path"] == f"{ROOM_PATH}/event/$new"
    assert endpoint.writes()[0]["body"] == {"pinned": ["$old", "$new"], "extra": 1}


async def test_pin_without_existing_pins_starts_a_list(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$new")] = ({"event_id": "$new"}, 200)
    endpoint.responses[("PUT", PINNED_PATH)] = ({"event_id": "$pins"}, 200)
    assert (await moderation.pin(ROOM, "$new")).pinned == ["$new"]
    assert endpoint.writes()[0]["body"] == {"pinned": ["$new"]}


async def test_pin_is_idempotent_without_writing(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", f"{ROOM_PATH}/event/$old")] = ({"event_id": "$old"}, 200)
    endpoint.responses[("GET", PINNED_PATH)] = ({"pinned": ["$old"]}, 200)
    result = await moderation.pin(ROOM, "$old")
    assert result.pinned == ["$old"]
    assert result.changed is False
    assert endpoint.writes() == []


async def test_pin_refuses_events_the_room_does_not_have(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    with pytest.raises(MatrixHTTPError, match="M_NOT_FOUND"):
        await moderation.pin(ROOM, "$missing")
    assert endpoint.writes() == []


async def test_unpin_removes_every_occurrence(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", PINNED_PATH)] = ({"pinned": ["$a", "$b", "$a"]}, 200)
    endpoint.responses[("PUT", PINNED_PATH)] = ({"event_id": "$pins"}, 200)
    result = await moderation.unpin(ROOM, "$a")
    assert result.pinned == ["$b"]
    assert result.changed is True
    assert endpoint.writes()[0]["body"] == {"pinned": ["$b"]}
    assert all("/event/" not in request["path"] for request in endpoint.requests)


async def test_unpin_is_idempotent_without_writing(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    result = await moderation.unpin(ROOM, "$a")
    assert result.pinned == []
    assert result.changed is False
    assert endpoint.writes() == []


async def test_pin_writes_propagate_permission_errors(
    matrix: tuple[MatrixModeration, RoomEndpoint],
) -> None:
    moderation, endpoint = matrix
    endpoint.responses[("GET", PINNED_PATH)] = ({"pinned": ["$a"]}, 200)
    endpoint.responses[("PUT", PINNED_PATH)] = ({"errcode": "M_FORBIDDEN"}, 403)
    with pytest.raises(MatrixHTTPError, match="M_FORBIDDEN"):
        await moderation.unpin(ROOM, "$a")
