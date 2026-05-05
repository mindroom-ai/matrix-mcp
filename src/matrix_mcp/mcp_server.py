from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP

from matrix_mcp.matrix_client import MatrixAPIClient, MatrixDriver, MatrixEvent, MatrixRoom

if TYPE_CHECKING:
    from collections.abc import Callable


class MatrixMCPTools:
    def __init__(self, client_factory: Callable[[], MatrixDriver] = MatrixAPIClient) -> None:
        self._client_factory = client_factory

    async def matrix_whoami(self) -> dict[str, str | None]:
        """Return the Matrix user and device for the configured session."""
        return await self._client_factory().whoami()

    async def matrix_list_rooms(self) -> list[MatrixRoom]:
        """List joined Matrix rooms visible to the authenticated user."""
        return await self._client_factory().list_rooms()

    async def matrix_read_room_recent(self, room_id: str, limit: int = 20) -> list[MatrixEvent]:
        """Read recent text messages from one Matrix room."""
        return await self._client_factory().read_room_recent(room_id, limit=limit)

    async def matrix_send_message(
        self,
        room_id: str,
        body: str,
        thread_id: str | None = None,
    ) -> dict[str, str]:
        """Send a Matrix text message, optionally as a thread reply."""
        event_id = await self._client_factory().send_message(room_id, body, thread_id=thread_id)
        return {"event_id": event_id}


def create_mcp_server(client_factory: Callable[[], MatrixDriver] = MatrixAPIClient) -> FastMCP:
    mcp = FastMCP(
        "matrix-mcp",
        instructions=(
            "Use these tools to inspect and participate in Matrix conversations. "
            "Prefer read tools first. Use send tools only when the user explicitly asks to post."
        ),
    )
    tools = MatrixMCPTools(client_factory=client_factory)
    mcp.tool(tools.matrix_whoami)
    mcp.tool(tools.matrix_list_rooms)
    mcp.tool(tools.matrix_read_room_recent)
    mcp.tool(tools.matrix_send_message)
    return mcp


mcp = create_mcp_server()
