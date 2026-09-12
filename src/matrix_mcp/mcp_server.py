from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Protocol

from fastmcp import FastMCP
from pydantic import Field

from matrix_mcp.matrix_client import (
    MatrixAPIClient,
    MatrixEvent,
    MatrixProfile,
    MatrixRoom,
    MatrixRoomInfo,
    MatrixRoomMembers,
    MatrixUserSearch,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from matrix_mcp.hosted_server import HostedMatrixTools


class MatrixMCPClient(Protocol):
    async def whoami(self) -> dict[str, str | None]: ...

    async def list_rooms(self) -> list[MatrixRoom]: ...

    async def list_room_members(
        self, room_id: str | int, *, limit: int = 100, offset: int = 0
    ) -> MatrixRoomMembers: ...

    async def invite_user(self, room_id: str | int, user_id: str) -> None: ...

    async def get_room_info(self, room_id: str | int) -> MatrixRoomInfo: ...

    async def set_room_name(self, room_id: str | int, name: str) -> str: ...

    async def set_room_topic(self, room_id: str | int, topic: str) -> str: ...

    async def set_room_avatar(self, room_id: str | int, avatar_url: str) -> str: ...

    async def get_profile(self, user_id: str | None = None) -> MatrixProfile: ...

    async def set_display_name(self, displayname: str) -> None: ...

    async def set_avatar(self, avatar_url: str) -> None: ...

    async def search_users(self, search_term: str, *, limit: int = 25) -> MatrixUserSearch: ...

    async def read_room_recent(
        self, room_id: str | int, *, limit: int = 20
    ) -> list[MatrixEvent]: ...

    async def read_thread(
        self, room_id: str | int, thread_id: str | int, *, limit: int = 50
    ) -> list[MatrixEvent]: ...

    async def send_message(
        self,
        room_id: str | int,
        body: str,
        *,
        thread_id: str | int | None = None,
        mentions: list[str] | None = None,
    ) -> str: ...

    async def send_file(
        self,
        room_id: str | int,
        file_path: str,
        *,
        thread_id: str | int | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str: ...


class MatrixMCPTools:
    def __init__(self, client_factory: Callable[[], MatrixMCPClient] = MatrixAPIClient) -> None:
        self._client_factory = client_factory

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[MatrixMCPClient]:
        client = self._client_factory()
        try:
            yield client
        finally:
            if isinstance(client, MatrixAPIClient):
                await client.aclose()

    async def matrix_whoami(self) -> dict[str, str | None]:
        """Return the Matrix user and device for the configured session."""
        async with self._client() as client:
            return await client.whoami()

    async def matrix_list_rooms(self) -> list[MatrixRoom]:
        """List joined Matrix rooms visible to the authenticated user."""
        async with self._client() as client:
            return await client.list_rooms()

    async def matrix_list_room_members(
        self,
        room_id: str | int,
        limit: Annotated[int, Field(ge=1, le=100)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> MatrixRoomMembers:
        """List joined members with Matrix IDs, display names, and avatars.

        Follow next_offset for another page; invited or departed users are excluded.
        """
        async with self._client() as client:
            return await client.list_room_members(room_id, limit=limit, offset=offset)

    async def matrix_invite_user(self, room_id: str | int, user_id: str) -> dict[str, str]:
        """Invite a full Matrix user ID to a room when the user requests it."""
        async with self._client() as client:
            await client.invite_user(room_id, user_id)
        return {"status": "invited"}

    async def matrix_get_room_info(self, room_id: str | int) -> MatrixRoomInfo:
        """Read the room name, topic, and avatar before changing room details."""
        async with self._client() as client:
            return await client.get_room_info(room_id)

    async def matrix_set_room_name(self, room_id: str | int, name: str) -> dict[str, str]:
        """Change a room's name; an empty string clears it. Requires room permission."""
        async with self._client() as client:
            return {"event_id": await client.set_room_name(room_id, name)}

    async def matrix_set_room_topic(self, room_id: str | int, topic: str) -> dict[str, str]:
        """Change a room's topic; an empty string clears it. Requires room permission."""
        async with self._client() as client:
            return {"event_id": await client.set_room_topic(room_id, topic)}

    async def matrix_set_room_avatar(self, room_id: str | int, avatar_url: str) -> dict[str, str]:
        """Set a room avatar from an existing mxc:// media URI, or clear it with an empty string."""
        async with self._client() as client:
            return {"event_id": await client.set_room_avatar(room_id, avatar_url)}

    async def matrix_get_profile(self, user_id: str | None = None) -> MatrixProfile:
        """Read a Matrix profile; omit user_id to read the connected user's profile."""
        async with self._client() as client:
            return await client.get_profile(user_id)

    async def matrix_set_display_name(self, displayname: str) -> dict[str, str]:
        """Change the connected user's global display name; an empty string clears it."""
        async with self._client() as client:
            await client.set_display_name(displayname)
        return {"status": "updated"}

    async def matrix_set_avatar(self, avatar_url: str) -> dict[str, str]:
        """Set your global avatar from mxc:// media, or clear it with an empty string."""
        async with self._client() as client:
            await client.set_avatar(avatar_url)
        return {"status": "updated"}

    async def matrix_search_users(
        self, search_term: str, limit: Annotated[int, Field(ge=1, le=100)] = 25
    ) -> MatrixUserSearch:
        """Find Matrix user IDs by name or ID using the homeserver's visible user directory."""
        async with self._client() as client:
            return await client.search_users(search_term, limit=limit)

    async def matrix_read_room_recent(
        self, room_id: str | int, limit: int = 20
    ) -> list[MatrixEvent]:
        """Read recent text messages from one Matrix room by Matrix room ID or numeric room ref."""
        async with self._client() as client:
            return await client.read_room_recent(room_id, limit=limit)

    async def matrix_read_thread(
        self,
        room_id: str | int,
        thread_id: str | int,
        limit: int = 50,
    ) -> list[MatrixEvent]:
        """Read a Matrix thread root and its recent text replies by Matrix ID or numeric ref."""
        async with self._client() as client:
            return await client.read_thread(room_id, thread_id, limit=limit)

    async def matrix_send_message(
        self,
        room_id: str | int,
        body: str | None = None,
        thread_id: str | int | None = None,
        file_path: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        mentions: list[str] | None = None,
    ) -> dict[str, str]:
        """Send text or a local file, with optional thread and explicit text mentions."""
        if file_path:
            if mentions is not None:
                msg = "mentions are supported only for text messages"
                raise ValueError(msg)
            async with self._client() as client:
                event_id = await client.send_file(
                    room_id,
                    file_path,
                    thread_id=thread_id,
                    filename=filename,
                    content_type=content_type,
                )
            return {"event_id": event_id}
        if body is None:
            msg = "matrix_send_message requires either body or file_path"
            raise ValueError(msg)
        async with self._client() as client:
            event_id = await client.send_message(
                room_id,
                body,
                thread_id=thread_id,
                mentions=mentions,
            )
        return {"event_id": event_id}


def create_mcp_server(client_factory: Callable[[], MatrixMCPClient] = MatrixAPIClient) -> FastMCP:
    mcp = FastMCP(
        "matrix-mcp",
        instructions=(
            "Use these tools to inspect and participate in Matrix conversations. "
            "Read and list tools return stable numeric refs; prefer those refs in later calls "
            "instead of raw Matrix IDs. "
            "Prefer read tools first. Send messages, invite users, or change room/profile details "
            "only when the user explicitly requests that action."
        ),
    )
    tools = MatrixMCPTools(client_factory=client_factory)
    mcp.tool(tools.matrix_whoami)
    mcp.tool(tools.matrix_list_rooms)
    mcp.tool(tools.matrix_read_room_recent)
    mcp.tool(tools.matrix_read_thread)
    mcp.tool(tools.matrix_send_message)
    register_room_profile_tools(mcp, tools)
    return mcp


def register_room_profile_tools(server: FastMCP, tools: MatrixMCPTools | HostedMatrixTools) -> None:
    """Register the shared room/profile surface with accurate operation hints."""
    for name in (
        "matrix_list_room_members",
        "matrix_get_room_info",
        "matrix_get_profile",
        "matrix_search_users",
    ):
        server.tool(getattr(tools, name), annotations={"readOnlyHint": True})
    server.tool(
        tools.matrix_invite_user,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
    for name in (
        "matrix_set_room_name",
        "matrix_set_room_topic",
        "matrix_set_room_avatar",
        "matrix_set_display_name",
        "matrix_set_avatar",
    ):
        server.tool(
            getattr(tools, name),
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
        )


mcp = create_mcp_server()
