"""Shared Matrix conversation tools for stdio and hosted transports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Protocol

from fastmcp import FastMCP  # noqa: TC002 - FastMCP resolves tool annotations at runtime.
from pydantic import BaseModel, ConfigDict, Field

from matrix_mcp.matrix_events import EventContext, HistoryPage, MatrixEvents  # noqa: TC001
from matrix_mcp.matrix_media import (
    MAX_SAFE_JSON_INTEGER,
    MXC_URI_PATTERN,
    DownloadedMedia,
    MatrixMedia,
    UploadedMedia,
)
from matrix_mcp.matrix_rooms import InvitationPage, MatrixRooms, UnreadPage  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager


class ConversationClient(Protocol):
    @property
    def events(self) -> MatrixEvents: ...

    @property
    def rooms(self) -> MatrixRooms: ...

    @property
    def media(self) -> MatrixMedia: ...


RoomID = Annotated[str, Field(pattern=r"^![^\s:/?#]+(?::[^\s/?#]+)?$")]
RoomIDOrAlias = Annotated[
    str,
    Field(pattern=r"^(?:![^\s:/?#]+(?::[^\s/?#]+)?|#[^\s:]+:[^\s/?#]+)$"),
]
EventID = Annotated[str, Field(pattern=r"^\$[^\s]+$")]
UserID = Annotated[str, Field(pattern=r"^@[^\s:]+:[^\s/?#@]+$")]
MediaURL = Annotated[str, Field(pattern=MXC_URI_PATTERN)]
TransactionID = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S+$")]
Filename = Annotated[str, Field(min_length=1, pattern=r"^[^\x00-\x1f\x7f/\\]+$")]
ContentType = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"),
]
MediaBase64 = Annotated[str, Field(max_length=6_990_508)]


class EventActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str


class RoomActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    room_id: str


class StatusResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str


class ConversationTools:
    """Tool wrappers around request-scoped or locally owned Matrix clients."""

    def __init__(
        self,
        client: Callable[[], AbstractAsyncContextManager[ConversationClient]],
    ) -> None:
        self.client = client

    async def matrix_read_history(
        self,
        room_id: RoomID,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        before: str | None = None,
    ) -> HistoryPage:
        """Read a newest-first history page from a raw room ID without marking it read."""
        async with self.client() as client:
            return await client.events.history(room_id, limit=limit, before=before)

    async def matrix_get_event_context(
        self,
        room_id: RoomID,
        event_id: EventID,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> EventContext:
        """Read one event and bounded surrounding events without marking the room read."""
        async with self.client() as client:
            return await client.events.context(room_id, event_id, limit=limit)

    async def matrix_reply(
        self,
        room_id: RoomID,
        event_id: EventID,
        body: Annotated[str, Field(min_length=1)],
        transaction_id: TransactionID | None = None,
    ) -> EventActionResult:
        """Reply as the connected user only when the user explicitly requests a reply."""
        async with self.client() as client:
            result = await client.events.reply(
                room_id,
                event_id,
                body,
                transaction_id=transaction_id,
            )
        return EventActionResult(event_id=result)

    async def matrix_react(
        self,
        room_id: RoomID,
        event_id: EventID,
        key: Annotated[str, Field(min_length=1)],
        transaction_id: TransactionID | None = None,
    ) -> EventActionResult:
        """React as the connected user only when the user explicitly requests a reaction."""
        async with self.client() as client:
            result = await client.events.react(
                room_id,
                event_id,
                key,
                transaction_id=transaction_id,
            )
        return EventActionResult(event_id=result)

    async def matrix_edit_message(
        self,
        room_id: RoomID,
        event_id: EventID,
        body: Annotated[str, Field(min_length=1)],
        transaction_id: TransactionID | None = None,
    ) -> EventActionResult:
        """Edit an owned message only when the user explicitly requests that change."""
        async with self.client() as client:
            result = await client.events.edit(
                room_id,
                event_id,
                body,
                transaction_id=transaction_id,
            )
        return EventActionResult(event_id=result)

    async def matrix_redact_event(
        self,
        room_id: RoomID,
        event_id: EventID,
        reason: str | None = None,
        transaction_id: TransactionID | None = None,
    ) -> EventActionResult:
        """Redact an owned event only when the user explicitly requests its removal."""
        async with self.client() as client:
            result = await client.events.redact(
                room_id,
                event_id,
                reason=reason,
                transaction_id=transaction_id,
            )
        return EventActionResult(event_id=result)

    async def matrix_list_invitations(
        self,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> InvitationPage:
        """List current room invitations without joining or declining them."""
        async with self.client() as client:
            return await client.rooms.invitations(limit=limit, offset=offset)

    async def matrix_join_room(self, room_id_or_alias: RoomIDOrAlias) -> RoomActionResult:
        """Join a room only when the user explicitly requests it, using a raw ID or alias."""
        async with self.client() as client:
            result = await client.rooms.join(room_id_or_alias)
        return RoomActionResult(room_id=result)

    async def matrix_leave_room(
        self,
        room_id: RoomID,
        reason: str | None = None,
    ) -> StatusResult:
        """Leave or decline a room only when the user explicitly requests it."""
        async with self.client() as client:
            await client.rooms.leave(room_id, reason=reason)
        return StatusResult(status="left")

    async def matrix_create_room(
        self,
        name: str | None = None,
        topic: str | None = None,
        invite: Annotated[list[UserID] | None, Field(max_length=100)] = None,
    ) -> RoomActionResult:
        """Create a private room only when the user explicitly requests one."""
        async with self.client() as client:
            result = await client.rooms.create(name=name, topic=topic, invite=invite)
        return RoomActionResult(room_id=result)

    async def matrix_get_unread(
        self,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        timeline_limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> UnreadPage:
        """Read a fresh unread snapshot without marking anything read.

        Counts depend on homeserver push rules, recent mentions are bounded, and pages can
        change concurrently.
        """
        async with self.client() as client:
            return await client.rooms.unread(
                limit=limit,
                offset=offset,
                timeline_limit=timeline_limit,
            )

    async def matrix_mark_read(
        self,
        room_id: RoomID,
        event_id: EventID,
        public_receipt: bool = False,  # noqa: FBT001, FBT002 - MCP exposes a named flag.
    ) -> StatusResult:
        """Mark through an event read only when explicitly requested; defaults to private."""
        async with self.client() as client:
            await client.rooms.mark_read(
                room_id,
                event_id,
                public_receipt=public_receipt,
            )
        return StatusResult(status="read")

    async def matrix_upload_media(
        self,
        data_base64: MediaBase64,
        filename: Filename,
        content_type: ContentType = "application/octet-stream",
    ) -> UploadedMedia:
        """Upload base64 media only when explicitly requested; accepts no local file path."""
        async with self.client() as client:
            return await client.media.upload(
                data_base64,
                filename,
                content_type=content_type,
            )

    async def matrix_download_media(self, media_url: MediaURL) -> DownloadedMedia:
        """Download bounded media from an mxc URI without accepting an HTTP URL or path."""
        async with self.client() as client:
            return await client.media.download(media_url)

    async def matrix_send_media(  # noqa: PLR0913 - MCP exposes attachment metadata directly.
        self,
        room_id: RoomID,
        media_url: MediaURL,
        filename: Filename,
        content_type: ContentType = "application/octet-stream",
        size: Annotated[int | None, Field(ge=0, le=MAX_SAFE_JSON_INTEGER)] = None,
        thread_id: EventID | None = None,
        transaction_id: TransactionID | None = None,
    ) -> EventActionResult:
        """Send uploaded media only when explicitly requested, optionally in a thread."""
        async with self.client() as client:
            result = await client.media.send(
                room_id,
                media_url,
                filename,
                content_type=content_type,
                size=size,
                thread_id=thread_id,
                transaction_id=transaction_id,
            )
        return EventActionResult(event_id=result)


def register_conversation_tools(server: FastMCP, tools: ConversationTools) -> None:
    """Register the shared conversation surface with operation hints."""
    for name in (
        "matrix_read_history",
        "matrix_get_event_context",
        "matrix_list_invitations",
        "matrix_get_unread",
        "matrix_download_media",
    ):
        server.tool(getattr(tools, name), annotations={"readOnlyHint": True})

    nondestructive = (
        "matrix_reply",
        "matrix_react",
        "matrix_join_room",
        "matrix_create_room",
        "matrix_mark_read",
        "matrix_upload_media",
        "matrix_send_media",
    )
    for name in nondestructive:
        server.tool(
            getattr(tools, name),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": name == "matrix_mark_read",
            },
        )

    for name in ("matrix_edit_message", "matrix_redact_event", "matrix_leave_room"):
        server.tool(
            getattr(tools, name),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": name == "matrix_leave_room",
            },
        )
