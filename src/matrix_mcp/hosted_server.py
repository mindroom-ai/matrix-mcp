"""Authenticated HTTP tools with request-local Matrix clients and raw IDs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from pydantic import Field

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.conversation_tools import (
    ConversationTools,
    EventID,
    RoomID,
    UserID,
    register_conversation_tools,
)
from matrix_mcp.hosted_auth import HostedSettings, MatrixOAuthProvider
from matrix_mcp.matrix_client import (
    MatrixAPIClient,
    MatrixEvent,
    MatrixProfile,
    MatrixRoom,
    MatrixRoomInfo,
    MatrixRoomMembers,
    MatrixUserSearch,
    NioMatrixDriver,
)
from matrix_mcp.mcp_server import register_room_profile_tools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class HostedMatrixTools:
    def __init__(self, settings: HostedSettings) -> None:
        self.settings = settings

    @asynccontextmanager
    async def client(self) -> AsyncIterator[MatrixAPIClient]:
        token = get_access_token()
        if token is None or "matrix" not in token.scopes:
            msg = "An authenticated Matrix connection is required"
            raise RuntimeError(msg)
        config = MatrixMCPConfig(
            homeserver=self.settings.matrix_api_url,
            access_token=token.token,
            user_id=token.claims["user_id"],
            device_id=token.claims["device_id"],
        )
        driver = NioMatrixDriver(config)
        try:
            # Inject the driver explicitly: no config fallback or numeric ID store.
            yield MatrixAPIClient(driver=driver)
        finally:
            await driver.close()

    async def matrix_whoami(self) -> dict[str, str | None]:
        """Return the connected Matrix user and device."""
        async with self.client() as client:
            return await client.whoami()

    async def matrix_list_rooms(self) -> list[MatrixRoom]:
        """List rooms visible to the connected Matrix account, using raw Matrix IDs."""
        async with self.client() as client:
            return await client.list_rooms()

    async def matrix_list_room_members(
        self,
        room_id: RoomID,
        limit: Annotated[int, Field(ge=1, le=100)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> MatrixRoomMembers:
        """List joined members with user IDs, display names, and avatars; follow next_offset.

        Invited and departed users are excluded. Use a raw Matrix room ID.
        """
        async with self.client() as client:
            return await client.list_room_members(room_id, limit=limit, offset=offset)

    async def matrix_invite_user(self, room_id: RoomID, user_id: UserID) -> dict[str, str]:
        """Invite a Matrix user as the connected account when explicitly requested."""
        async with self.client() as client:
            await client.invite_user(room_id, user_id)
        return {"status": "invited"}

    async def matrix_get_room_info(self, room_id: RoomID) -> MatrixRoomInfo:
        """Read the room name, topic, and avatar before changing room details."""
        async with self.client() as client:
            return await client.get_room_info(room_id)

    async def matrix_set_room_name(self, room_id: RoomID, name: str) -> dict[str, str]:
        """Change a room's name; an empty string clears it. Requires room permission."""
        async with self.client() as client:
            return {"event_id": await client.set_room_name(room_id, name)}

    async def matrix_set_room_topic(self, room_id: RoomID, topic: str) -> dict[str, str]:
        """Change a room's topic; an empty string clears it. Requires room permission."""
        async with self.client() as client:
            return {"event_id": await client.set_room_topic(room_id, topic)}

    async def matrix_set_room_avatar(self, room_id: RoomID, avatar_url: str) -> dict[str, str]:
        """Set a room avatar from an existing mxc:// media URI, or clear with an empty string."""
        async with self.client() as client:
            return {"event_id": await client.set_room_avatar(room_id, avatar_url)}

    async def matrix_get_profile(self, user_id: UserID | None = None) -> MatrixProfile:
        """Read a Matrix profile; omit user_id to read the connected user's profile."""
        async with self.client() as client:
            return await client.get_profile(user_id)

    async def matrix_set_display_name(self, displayname: str) -> dict[str, str]:
        """Change the connected user's global display name; an empty string clears it."""
        async with self.client() as client:
            await client.set_display_name(displayname)
        return {"status": "updated"}

    async def matrix_set_avatar(self, avatar_url: str) -> dict[str, str]:
        """Set your global avatar from mxc:// media, or clear it with an empty string."""
        async with self.client() as client:
            await client.set_avatar(avatar_url)
        return {"status": "updated"}

    async def matrix_search_users(
        self, search_term: str, limit: Annotated[int, Field(ge=1, le=100)] = 25
    ) -> MatrixUserSearch:
        """Find user IDs by name or ID using the homeserver's visible user directory."""
        async with self.client() as client:
            return await client.search_users(search_term, limit=limit)

    async def matrix_read_room_recent(self, room_id: RoomID, limit: int = 20) -> list[MatrixEvent]:
        """Read recent text messages from a raw Matrix room ID."""
        async with self.client() as client:
            return await client.read_room_recent(room_id, limit=limit)

    async def matrix_read_thread(
        self, room_id: RoomID, thread_id: EventID, limit: int = 50
    ) -> list[MatrixEvent]:
        """Read a thread root and recent replies using raw Matrix IDs."""
        async with self.client() as client:
            return await client.read_thread(room_id, thread_id, limit=limit)

    async def matrix_send_message(
        self,
        room_id: RoomID,
        body: str,
        thread_id: EventID | None = None,
        mentions: list[UserID] | None = None,
    ) -> dict[str, str]:
        """Send plaintext as the connected user, with optional thread and explicit mentions.

        The encryption preflight is best effort. A room enabling encryption between
        the check and send can receive plaintext. Do not use where E2EE is required.
        """
        await self._require_unencrypted_room(room_id)
        async with self.client() as client:
            return {
                "event_id": await client.send_message(
                    room_id,
                    body,
                    thread_id=thread_id,
                    mentions=mentions,
                )
            }

    async def _require_unencrypted_room(self, room_id: str) -> None:
        token = get_access_token()
        if token is None:
            msg = "An authenticated Matrix connection is required"
            raise RuntimeError(msg)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(
                f"{self.settings.matrix_api_url}/_matrix/client/v3/rooms/"
                f"{quote(room_id, safe='')}/state/m.room.encryption",
                headers={"Authorization": f"Bearer {token.token}"},
            )
        if (
            response.status_code == HTTPStatus.NOT_FOUND
            and response.json().get("errcode") == "M_NOT_FOUND"
        ):
            return
        msg = "Text send refused: room is encrypted or encryption state could not be verified"
        raise RuntimeError(msg)


def create_hosted_server(settings: HostedSettings) -> FastMCP:
    provider = MatrixOAuthProvider(settings)
    server = FastMCP(
        "matrix-mcp",
        auth=provider,
        lifespan=provider.lifespan,
        instructions=(
            "Use raw Matrix room and event IDs. Read tools first. "
            "Send text, invite users, or change room/profile details only when the user "
            "explicitly requests that action."
        ),
    )
    tools = HostedMatrixTools(settings)
    server.tool(tools.matrix_whoami)
    server.tool(tools.matrix_list_rooms)
    server.tool(tools.matrix_read_room_recent)
    server.tool(tools.matrix_read_thread)
    server.tool(tools.matrix_send_message)
    register_room_profile_tools(server, tools)
    register_conversation_tools(server, ConversationTools(tools.client))
    return server
