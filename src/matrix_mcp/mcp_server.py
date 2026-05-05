from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastmcp import FastMCP

from matrix_mcp.matrix_client import MatrixAPIClient, MatrixEvent, MatrixRoom

if TYPE_CHECKING:
    from collections.abc import Callable


class MatrixMCPClient(Protocol):
    async def whoami(self) -> dict[str, str | None]: ...

    async def list_rooms(self) -> list[MatrixRoom]: ...

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

    async def matrix_whoami(self) -> dict[str, str | None]:
        """Return the Matrix user and device for the configured session."""
        return await self._client_factory().whoami()

    async def matrix_list_rooms(self) -> list[MatrixRoom]:
        """List joined Matrix rooms visible to the authenticated user."""
        return await self._client_factory().list_rooms()

    async def matrix_read_room_recent(
        self, room_id: str | int, limit: int = 20
    ) -> list[MatrixEvent]:
        """Read recent text messages from one Matrix room by Matrix room ID or numeric room ref."""
        return await self._client_factory().read_room_recent(room_id, limit=limit)

    async def matrix_read_thread(
        self,
        room_id: str | int,
        thread_id: str | int,
        limit: int = 50,
    ) -> list[MatrixEvent]:
        """Read a Matrix thread root and its recent text replies by Matrix ID or numeric ref."""
        return await self._client_factory().read_thread(room_id, thread_id, limit=limit)

    async def matrix_send_message(
        self,
        room_id: str | int,
        body: str | None = None,
        thread_id: str | int | None = None,
        file_path: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, str]:
        """Send text or a local file by Matrix ID or numeric ref, optionally as a thread reply."""
        if file_path:
            event_id = await self._client_factory().send_file(
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
        event_id = await self._client_factory().send_message(room_id, body, thread_id=thread_id)
        return {"event_id": event_id}


def create_mcp_server(client_factory: Callable[[], MatrixMCPClient] = MatrixAPIClient) -> FastMCP:
    mcp = FastMCP(
        "matrix-mcp",
        instructions=(
            "Use these tools to inspect and participate in Matrix conversations. "
            "Read and list tools return stable numeric refs; prefer those refs in later calls "
            "instead of raw Matrix IDs. "
            "Prefer read tools first. Use send tools only when the user explicitly asks to post."
        ),
    )
    tools = MatrixMCPTools(client_factory=client_factory)
    mcp.tool(tools.matrix_whoami)
    mcp.tool(tools.matrix_list_rooms)
    mcp.tool(tools.matrix_read_room_recent)
    mcp.tool(tools.matrix_read_thread)
    mcp.tool(tools.matrix_send_message)
    return mcp


mcp = create_mcp_server()
