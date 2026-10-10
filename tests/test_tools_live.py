"""Room, search, and moderation tools against a real homeserver.

Opt in by pointing the test at a disposable homeserver that allows registration
with a token:

    MATRIX_MCP_LIVE_HOMESERVER=http://127.0.0.1:8008 \
    MATRIX_MCP_LIVE_REGISTRATION_TOKEN=... uv run pytest tests/test_tools_live.py
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest
from fastmcp import Client

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.id_state import MatrixIdStore
from matrix_mcp.matrix_client import MatrixAPIClient
from matrix_mcp.mcp_server import create_mcp_server

if TYPE_CHECKING:
    from pathlib import Path

HOMESERVER = os.environ.get("MATRIX_MCP_LIVE_HOMESERVER", "")
REGISTRATION_TOKEN = os.environ.get("MATRIX_MCP_LIVE_REGISTRATION_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not HOMESERVER or not REGISTRATION_TOKEN,
    reason="set MATRIX_MCP_LIVE_HOMESERVER and MATRIX_MCP_LIVE_REGISTRATION_TOKEN",
)


async def register(name: str) -> dict[str, str]:
    body: dict[str, Any] = {"username": name, "password": f"password-{name}"}
    async with httpx.AsyncClient(base_url=HOMESERVER) as http:
        challenge = await http.post("/_matrix/client/v3/register", json=body)
        session = challenge.json()["session"]
        for stage in ("m.login.registration_token", "m.login.dummy"):
            auth: dict[str, str] = {"type": stage, "session": session}
            if stage == "m.login.registration_token":
                auth["token"] = REGISTRATION_TOKEN
            response = await http.post("/_matrix/client/v3/register", json={**body, "auth": auth})
            if response.status_code == httpx.codes.OK:
                return dict(response.json())
    msg = f"Could not register {name}: {response.text}"
    raise RuntimeError(msg)


async def request(
    account: dict[str, str], method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Act as another Matrix user, such as a room member using their own chat app."""
    async with httpx.AsyncClient(base_url=HOMESERVER) as http:
        response = await http.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {account['access_token']}"},
        )
    response.raise_for_status()
    return dict(response.json())


async def send(account: dict[str, str], room_id: str, content: dict[str, Any], kind: str) -> str:
    path = f"/_matrix/client/v3/rooms/{quote(room_id)}/send/{kind}/{uuid4().hex}"
    return str((await request(account, "PUT", path, content))["event_id"])


async def call(client: Client[Any], name: str, arguments: dict[str, Any]) -> Any:
    result = await client.call_tool(name, arguments)
    return result.structured_content


async def test_room_search_and_moderation_tools_against_a_real_homeserver(  # noqa: PLR0915 - One scenario shares its accounts and rooms.
    tmp_path: Path,
) -> None:
    suffix = uuid4().hex[:8]
    alice = await register(f"alice{suffix}")
    bob = await register(f"bob{suffix}")
    config = MatrixMCPConfig(
        homeserver=HOMESERVER,
        user_id=alice["user_id"],
        device_id=alice["device_id"],
        access_token=alice["access_token"],
    )
    server = create_mcp_server(
        lambda: MatrixAPIClient(config=config, id_store=MatrixIdStore(tmp_path / "ids.json"))
    )

    async with Client(server) as client:
        room = (
            await call(client, "matrix_create_room", {"name": "Live", "invite": [bob["user_id"]]})
        )["room_id"]
        await request(bob, "POST", f"/_matrix/client/v3/join/{quote(room)}", {})
        root = (
            await call(
                client,
                "matrix_send_message",
                {"room_id": room, "body": f"The release date is friday {suffix}"},
            )
        )["event_id"]
        reply = await send(
            bob,
            room,
            {
                "msgtype": "m.text",
                "body": "Friday works for me",
                "m.relates_to": {"rel_type": "m.thread", "event_id": root},
            },
            "m.room.message",
        )
        await send(
            bob,
            room,
            {"m.relates_to": {"rel_type": "m.annotation", "event_id": root, "key": "👍"}},
            "m.reaction",
        )
        await call(client, "matrix_react", {"room_id": room, "event_id": root, "key": "👍"})
        await request(
            bob,
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room)}/receipt/m.read/{quote(reply)}",
            {},
        )

        search = await call(client, "matrix_search_messages", {"search_term": suffix})
        assert [result["event"]["event_id"] for result in search["results"]] == [root]

        threads = await call(client, "matrix_list_threads", {"room_id": room})
        [thread] = threads["threads"]
        assert thread["root"]["event_id"] == root
        assert thread["reply_count"] == 1
        assert thread["latest_reply"]["event_id"] == reply

        reactions = await call(client, "matrix_get_reactions", {"room_id": room, "event_id": root})
        assert reactions["reactions"] == [
            {"key": "👍", "count": 2, "senders": sorted([alice["user_id"], bob["user_id"]])}
        ]

        receipts = await call(
            client, "matrix_get_read_receipts", {"room_id": room, "event_id": root}
        )
        bob_receipt = next(r for r in receipts["receipts"] if r["user_id"] == bob["user_id"])
        assert bob_receipt["event_id"] == reply
        assert bob_receipt["read"] is True

        pinned = await call(client, "matrix_pin_message", {"room_id": room, "event_id": root})
        assert pinned == {"pinned": [root], "changed": True}
        info = await call(client, "matrix_get_room_info", {"room_id": room})
        assert info["pinned_event_ids"] == [root]
        assert info["encrypted"] is False
        assert info["joined_member_count"] == 2
        # Room version 12 creators outrank every level, which is reported as null.
        assert info["own_power_level"] in {None, 100}
        unpinned = await call(client, "matrix_unpin_message", {"room_id": room, "event_id": root})
        assert unpinned == {"pinned": [], "changed": True}

        await call(
            client,
            "matrix_set_power_level",
            {"room_id": room, "user_id": bob["user_id"], "level": 50},
        )
        levels = await call(client, "matrix_get_power_levels", {"room_id": room})
        assert levels["users"][bob["user_id"]] == 50
        assert levels["own_level"] == info["own_power_level"]
        await call(
            client,
            "matrix_set_power_level",
            {"room_id": room, "user_id": bob["user_id"], "level": None},
        )
        levels = await call(client, "matrix_get_power_levels", {"room_id": room})
        assert bob["user_id"] not in levels["users"]

        await call(client, "matrix_kick_user", {"room_id": room, "user_id": bob["user_id"]})
        await call(client, "matrix_ban_user", {"room_id": room, "user_id": bob["user_id"]})
        member_path = f"/_matrix/client/v3/rooms/{quote(room)}/state/m.room.member/"
        banned = await request(alice, "GET", member_path + quote(bob["user_id"]))
        assert banned["membership"] == "ban"
        await call(client, "matrix_unban_user", {"room_id": room, "user_id": bob["user_id"]})
        unbanned = await request(alice, "GET", member_path + quote(bob["user_id"]))
        assert unbanned["membership"] == "leave"

        direct = await call(client, "matrix_create_dm", {"user_id": bob["user_id"]})
        assert direct["created"] is True
        again = await call(client, "matrix_create_dm", {"user_id": bob["user_id"]})
        assert again == {"room_id": direct["room_id"], "created": False}

        carol = await register(f"carol{suffix}")
        sealed = await call(
            client, "matrix_create_dm", {"user_id": carol["user_id"], "encrypted": True}
        )
        await call(client, "matrix_send_message", {"room_id": sealed["room_id"], "body": "psst"})
        stored = await request(
            alice,
            "GET",
            f"/_matrix/client/v3/rooms/{quote(sealed['room_id'])}/messages?dir=b&limit=1",
        )
        assert stored["chunk"][0]["type"] == "m.room.encrypted"
        recent = await call(client, "matrix_read_room_recent", {"room_id": sealed["room_id"]})
        assert [(event["body"], event["encrypted"]) for event in recent["result"][:1]] == [
            ("psst", True)
        ]

        space = (
            await request(
                alice,
                "POST",
                "/_matrix/client/v3/createRoom",
                {"name": "Space", "creation_content": {"type": "m.space"}},
            )
        )["room_id"]
        await request(
            alice,
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(space)}/state/m.space.child/{quote(room)}",
            {"via": [alice["user_id"].split(":", 1)[1]]},
        )
        hierarchy = await call(client, "matrix_get_space_hierarchy", {"space_id": space})
        rooms = {entry["room_id"]: entry for entry in hierarchy["rooms"]}
        assert rooms[space]["room_type"] == "m.space"
        assert rooms[space]["children"] == [room]
        assert rooms[room]["joined"] is True

        await call(client, "matrix_send_message", {"room_id": space, "body": "newest"})
        listed = await call(client, "matrix_list_rooms", {"sort": "activity"})
        ordered = [entry["room_id"] for entry in listed["result"]]
        assert ordered[0] == space
        assert ordered.index(room) < ordered.index(direct["room_id"])
