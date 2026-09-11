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
from matrix_mcp.hosted_auth import HostedSettings, MatrixOAuthProvider
from matrix_mcp.matrix_client import MatrixAPIClient, MatrixEvent, MatrixRoom, NioMatrixDriver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

RoomID = Annotated[str, Field(pattern=r"^![^\s:]+:[^\s]+$")]
EventID = Annotated[str, Field(pattern=r"^\$[^\s]+$")]
UserID = Annotated[str, Field(pattern=r"^@[^\s:]+:[^\s]+$")]


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
            "Send text only when the user explicitly asks to post."
        ),
    )
    tools = HostedMatrixTools(settings)
    server.tool(tools.matrix_whoami)
    server.tool(tools.matrix_list_rooms)
    server.tool(tools.matrix_read_room_recent)
    server.tool(tools.matrix_read_thread)
    server.tool(tools.matrix_send_message)
    return server
