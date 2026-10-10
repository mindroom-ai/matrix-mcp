"""Shared Matrix conversation tools for stdio and hosted transports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from fastmcp import FastMCP  # noqa: TC002 - FastMCP resolves tool annotations at runtime.
from pydantic import BaseModel, ConfigDict, Field

from matrix_mcp.matrix_events import (  # noqa: TC001
    EventContext,
    HistoryPage,
    MatrixEvents,
    ReactionSummary,
    SearchPage,
    ThreadPage,
)
from matrix_mcp.matrix_media import (
    MAX_SAFE_JSON_INTEGER,
    MXC_URI_PATTERN,
    DownloadedMedia,
    MatrixMedia,
    UploadedMedia,
)
from matrix_mcp.matrix_moderation import MatrixModeration, PinnedEvents, PowerLevels  # noqa: TC001
from matrix_mcp.matrix_rooms import (  # noqa: TC001
    DirectRoom,
    InvitationPage,
    MatrixRooms,
    ReceiptList,
    SpaceHierarchy,
    UnreadPage,
)

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

    @property
    def moderation(self) -> MatrixModeration: ...


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
Cursor = Annotated[str, Field(min_length=1)]
PowerLevel = Annotated[int, Field(ge=-(2**53) + 1, le=2**53 - 1)]


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
        """Read newest-first history without marking it read.

        Edits use valid server bundles, replacements returned in the page, and a bounded
        advertised recovery scan. Original content can remain when the homeserver omits both
        direct sources.
        """
        async with self.client() as client:
            return await client.events.history(room_id, limit=limit, before=before)

    async def matrix_get_event_context(
        self,
        room_id: RoomID,
        event_id: EventID,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> EventContext:
        """Read one event and bounded surrounding events without marking the room read.

        Edits use valid server bundles, replacements returned in the context, and a bounded
        advertised recovery scan. Original content can remain when the homeserver omits both
        direct sources.
        """
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

    async def matrix_search_messages(
        self,
        search_term: Annotated[str, Field(min_length=1)],
        room_id: RoomID | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        order_by: Literal["recent", "rank"] = "recent",
        next_batch: Cursor | None = None,
    ) -> SearchPage:
        """Search message text with the homeserver's full-text search, optionally in one room.

        Homeservers cannot index end-to-end encrypted rooms, so their messages never match.
        Pass next_batch unchanged for more results; read matches with matrix_get_event_context.
        """
        async with self.client() as client:
            return await client.events.search(
                search_term,
                room_id=room_id,
                limit=limit,
                order_by=order_by,
                next_batch=next_batch,
            )

    async def matrix_list_threads(
        self,
        room_id: RoomID,
        include: Literal["all", "participated"] = "all",
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        before: Cursor | None = None,
    ) -> ThreadPage:
        """List a room's threads, newest first, with reply counts and latest replies.

        Pass next_batch unchanged as before for older threads; read one with matrix_read_thread.
        """
        async with self.client() as client:
            return await client.events.threads(
                room_id,
                include=include,
                limit=limit,
                before=before,
            )

    async def matrix_get_reactions(
        self,
        room_id: RoomID,
        event_id: EventID,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> ReactionSummary:
        """Summarize reactions on an event: each key, how many users used it, and who."""
        async with self.client() as client:
            return await client.events.reactions(room_id, event_id, limit=limit)

    async def matrix_get_read_receipts(
        self,
        room_id: RoomID,
        event_id: EventID | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> ReceiptList:
        """Show each member's latest read receipt in a room, newest first.

        Pass event_id to check who has read up to that event. Members with private receipts
        or receipts turned off do not appear.
        """
        async with self.client() as client:
            return await client.rooms.receipts(room_id, event_id=event_id, limit=limit)

    async def matrix_get_space_hierarchy(
        self,
        space_id: RoomID,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        max_depth: Annotated[int, Field(ge=1, le=5)] = 1,
        next_batch: Cursor | None = None,
    ) -> SpaceHierarchy:
        """List the rooms in a space, with member counts and whether you have joined each."""
        async with self.client() as client:
            return await client.rooms.hierarchy(
                space_id,
                limit=limit,
                max_depth=max_depth,
                next_batch=next_batch,
            )

    async def matrix_get_power_levels(self, room_id: RoomID) -> PowerLevels:
        """Read a room's power levels: who can do what, and the connected user's own level."""
        async with self.client() as client:
            return await client.moderation.power_levels(room_id)

    async def matrix_list_invitations(
        self,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> InvitationPage:
        """List current invitations from a fresh bounded sync snapshot.

        Limit and offset page the snapshot output after download; they do not reduce its bytes.
        """
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

    async def matrix_create_dm(
        self,
        user_id: UserID,
        encrypted: bool = False,  # noqa: FBT001, FBT002 - MCP exposes a named flag.
    ) -> DirectRoom:
        """Open a direct chat with one user only when explicitly requested.

        Reuses an existing direct chat with that user when there is one. Encrypted direct
        chats need local mode.
        """
        async with self.client() as client:
            return await client.rooms.create_dm(user_id, encrypted=encrypted)

    async def matrix_pin_message(self, room_id: RoomID, event_id: EventID) -> PinnedEvents:
        """Pin a message in a room only when explicitly requested. Requires room permission."""
        async with self.client() as client:
            return await client.moderation.pin(room_id, event_id)

    async def matrix_unpin_message(self, room_id: RoomID, event_id: EventID) -> PinnedEvents:
        """Unpin a message in a room only when explicitly requested. Requires room permission."""
        async with self.client() as client:
            return await client.moderation.unpin(room_id, event_id)

    async def matrix_kick_user(
        self,
        room_id: RoomID,
        user_id: UserID,
        reason: str | None = None,
    ) -> StatusResult:
        """Remove a user from a room only when explicitly requested; they may rejoin if allowed."""
        async with self.client() as client:
            await client.moderation.kick(room_id, user_id, reason=reason)
        return StatusResult(status="kicked")

    async def matrix_ban_user(
        self,
        room_id: RoomID,
        user_id: UserID,
        reason: str | None = None,
    ) -> StatusResult:
        """Ban a user from a room only when explicitly requested; they stay out until unbanned."""
        async with self.client() as client:
            await client.moderation.ban(room_id, user_id, reason=reason)
        return StatusResult(status="banned")

    async def matrix_unban_user(
        self,
        room_id: RoomID,
        user_id: UserID,
        reason: str | None = None,
    ) -> StatusResult:
        """Lift a user's ban only when explicitly requested; it does not invite them back."""
        async with self.client() as client:
            await client.moderation.unban(room_id, user_id, reason=reason)
        return StatusResult(status="unbanned")

    async def matrix_set_power_level(
        self,
        room_id: RoomID,
        user_id: UserID,
        level: PowerLevel | None,
    ) -> EventActionResult:
        """Set a user's power level only when explicitly requested; null resets it to the default.

        Refuses to lower the connected user's own level, which only someone else could undo.
        """
        async with self.client() as client:
            result = await client.moderation.set_power_level(room_id, user_id, level)
        return EventActionResult(event_id=result)

    async def matrix_get_unread(
        self,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        timeline_limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> UnreadPage:
        """Read a fresh unread snapshot without marking anything read.

        Rooms require homeserver unread counts or a marked-unread flag. Mentions are bounded
        details within those rooms. Limit and offset page output after the bounded snapshot is
        downloaded, and pages can change concurrently.
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
        """Mark through an event read, clear its manual unread flag, and default to private."""
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
        """Upload base64 media, such as an avatar image, only when explicitly requested.

        Uploads are stored unencrypted on the homeserver. To share a file in an end-to-end
        encrypted room, use matrix_send_message with file_path instead (local mode).
        """
        async with self.client() as client:
            return await client.media.upload(
                data_base64,
                filename,
                content_type=content_type,
            )

    async def matrix_download_media(
        self,
        media_url: MediaURL,
        room_id: RoomID | None = None,
        event_id: EventID | None = None,
    ) -> DownloadedMedia:
        """Download bounded media from an mxc URI without accepting an HTTP URL or path.

        For an encrypted attachment (media.encrypted), also pass the room_id and event_id of
        its message so the file can be decrypted.
        """
        if room_id is None or event_id is None:
            if room_id is not None or event_id is not None:
                msg = "Pass room_id and event_id together"
                raise ValueError(msg)
            async with self.client() as client:
                return await client.media.download(media_url)
        async with self.client() as client:
            attachment = await client.events.attachment(room_id, event_id)
            return await client.media.download(media_url, attachment=attachment)

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
        """Send uploaded media only when explicitly requested, optionally in a thread.

        Refuses end-to-end encrypted rooms; use matrix_send_message with file_path there.
        """
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
        "matrix_search_messages",
        "matrix_list_threads",
        "matrix_get_reactions",
        "matrix_get_read_receipts",
        "matrix_get_space_hierarchy",
        "matrix_get_power_levels",
        "matrix_list_invitations",
        "matrix_get_unread",
        "matrix_download_media",
    ):
        server.tool(getattr(tools, name), annotations={"readOnlyHint": True})

    idempotent = {
        "matrix_mark_read",
        "matrix_pin_message",
        "matrix_unpin_message",
        "matrix_unban_user",
        "matrix_leave_room",
        "matrix_set_power_level",
    }
    nondestructive = (
        "matrix_reply",
        "matrix_react",
        "matrix_join_room",
        "matrix_create_room",
        "matrix_create_dm",
        "matrix_mark_read",
        "matrix_upload_media",
        "matrix_send_media",
        "matrix_pin_message",
        "matrix_unpin_message",
        "matrix_unban_user",
    )
    for name in nondestructive:
        server.tool(
            getattr(tools, name),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": name in idempotent,
            },
        )

    for name in (
        "matrix_edit_message",
        "matrix_redact_event",
        "matrix_leave_room",
        "matrix_kick_user",
        "matrix_ban_user",
        "matrix_set_power_level",
    ):
        server.tool(
            getattr(tools, name),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": name in idempotent,
            },
        )
