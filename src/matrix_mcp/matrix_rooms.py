from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError

from matrix_mcp.matrix_http import quote_matrix_id

if TYPE_CHECKING:
    from matrix_mcp.matrix_http import MatrixHTTP


_MAX_PAGE = 100
_MAX_INVITEES = 100
_SYNC_EVENT_FIELDS = [
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


class Invitation(BaseModel):
    room_id: str
    name: str | None = None
    inviter: str | None = None


class InvitationPage(BaseModel):
    rooms: list[Invitation]
    total: int
    next_offset: int | None


class MentionEvent(BaseModel):
    event_id: str
    sender: str
    body: str | None = None
    timestamp_ms: int | None = None


class UnreadRoom(BaseModel):
    room_id: str
    name: str | None = None
    notification_count: int = Field(
        description="Current homeserver count, which depends on push rules."
    )
    highlight_count: int = Field(
        description="Current homeserver highlight count, which depends on push rules."
    )
    marked_unread: bool
    mentions: list[MentionEvent] = Field(
        description="Explicit mentions in the bounded recent timeline, not a historical search."
    )
    limited: bool
    prev_batch: str | None


class UnreadPage(BaseModel):
    rooms: list[UnreadRoom]
    total: int
    next_offset: int | None = Field(
        description=(
            "Next offset, or null at the end; each page is a fresh snapshot "
            "and concurrent activity may change it."
        )
    )


class _EventBatch(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


class _Timeline(_EventBatch):
    limited: bool = False
    prev_batch: str | None = None


class _Counts(BaseModel):
    notification_count: int = Field(default=0, ge=0)
    highlight_count: int = Field(default=0, ge=0)


class _RoomData(BaseModel):
    state: _EventBatch = Field(default_factory=_EventBatch)
    invite_state: _EventBatch = Field(default_factory=_EventBatch)
    account_data: _EventBatch = Field(default_factory=_EventBatch)
    timeline: _Timeline = Field(default_factory=_Timeline)
    unread_notifications: _Counts = Field(default_factory=_Counts)


class _Rooms(BaseModel):
    join: dict[str, _RoomData] = Field(default_factory=dict)
    invite: dict[str, _RoomData] = Field(default_factory=dict)


class _Sync(BaseModel):
    next_batch: str
    rooms: _Rooms = Field(default_factory=_Rooms)


class MatrixRooms:
    def __init__(self, http: MatrixHTTP) -> None:
        self.http = http

    async def invitations(self, *, limit: int = 50, offset: int = 0) -> InvitationPage:
        _validate_page(limit, offset)
        snapshot = await self._sync(timeline_limit=1, include_invites=True)
        invitations = []
        for room_id, room in sorted(snapshot.rooms.invite.items()):
            events = room.invite_state.events
            membership = next(
                (
                    event
                    for event in events
                    if event.get("type") == "m.room.member"
                    and event.get("state_key") == self.http.user_id
                ),
                {},
            )
            invitations.append(
                Invitation(
                    room_id=room_id,
                    name=_state_value(events, "m.room.name", "name"),
                    inviter=_string(membership.get("sender")),
                )
            )
        return InvitationPage(
            rooms=invitations[offset : offset + limit],
            total=len(invitations),
            next_offset=_next_offset(len(invitations), offset, limit),
        )

    async def join(self, room_id_or_alias: str) -> str:
        target = _identifier(room_id_or_alias, sigils="!#")
        result = await self.http.json("POST", f"/_matrix/client/v3/join/{target}", body={})
        return _room_result(result)

    async def leave(self, room_id: str, *, reason: str | None = None) -> None:
        room = _identifier(room_id, sigils="!")
        body: dict[str, object] = {} if reason is None else {"reason": reason}
        await self.http.json("POST", f"/_matrix/client/v3/rooms/{room}/leave", body=body)

    async def create(
        self,
        *,
        name: str | None = None,
        topic: str | None = None,
        invite: list[str] | None = None,
    ) -> str:
        if invite is not None:
            if len(invite) > _MAX_INVITEES:
                msg = "At most 100 invitees are supported"
                raise ValueError(msg)
            for user_id in invite:
                _identifier(user_id, sigils="@")
        body: dict[str, object] = {"visibility": "private", "preset": "private_chat"}
        body.update(
            {
                key: value
                for key, value in (("name", name), ("topic", topic), ("invite", invite))
                if value is not None
            }
        )
        return _room_result(
            await self.http.json("POST", "/_matrix/client/v3/createRoom", body=body)
        )

    async def unread(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        timeline_limit: int = 20,
    ) -> UnreadPage:
        _validate_page(limit, offset)
        _validate_page(timeline_limit, 0)
        snapshot = await self._sync(timeline_limit=timeline_limit)
        rooms = []
        for room_id, room in sorted(snapshot.rooms.join.items()):
            counts = room.unread_notifications
            marked = any(
                event.get("type") == "m.marked_unread" and _content(event).get("unread") is True
                for event in room.account_data.events
            )
            if not (counts.notification_count or counts.highlight_count or marked):
                continue
            rooms.append(
                UnreadRoom(
                    room_id=room_id,
                    name=_state_value(
                        room.state.events + room.timeline.events, "m.room.name", "name"
                    ),
                    notification_count=counts.notification_count,
                    highlight_count=counts.highlight_count,
                    marked_unread=marked,
                    mentions=_mentions(room.timeline.events[:timeline_limit], self.http.user_id),
                    limited=room.timeline.limited or len(room.timeline.events) > timeline_limit,
                    prev_batch=room.timeline.prev_batch,
                )
            )
        return UnreadPage(
            rooms=rooms[offset : offset + limit],
            total=len(rooms),
            next_offset=_next_offset(len(rooms), offset, limit),
        )

    async def mark_read(
        self,
        room_id: str,
        event_id: str,
        *,
        public_receipt: bool = False,
    ) -> None:
        room = _identifier(room_id, sigils="!")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        original = await self.http.json("GET", f"/_matrix/client/v3/rooms/{room}/event/{event}")
        if original.get("event_id") != event_id:
            msg = "Matrix returned an invalid event for the read marker"
            raise RuntimeError(msg)
        receipt = "m.read" if public_receipt else "m.read.private"
        await self.http.json(
            "POST",
            f"/_matrix/client/v3/rooms/{room}/read_markers",
            body={
                "m.fully_read": event_id,
                receipt: event_id,
            },
        )
        user = _identifier(self.http.user_id, sigils="@")
        try:
            await self.http.json(
                "PUT",
                f"/_matrix/client/v3/user/{user}/rooms/{room}/account_data/m.marked_unread",
                body={"unread": False},
            )
        except RuntimeError:
            msg = (
                "Matrix read markers were updated, but the manual unread flag could not be "
                "cleared; it is safe to retry"
            )
            raise RuntimeError(msg) from None

    async def _sync(
        self,
        *,
        timeline_limit: int,
        include_invites: bool = False,
    ) -> _Sync:
        sync_filter = {
            "event_fields": _SYNC_EVENT_FIELDS,
            "presence": {"types": []},
            "account_data": {"types": []},
            "room": {
                "state": {
                    "types": ["m.room.name", "m.room.member"]
                    if include_invites
                    else ["m.room.name"],
                    "lazy_load_members": True,
                },
                "timeline": {
                    "limit": timeline_limit,
                    "types": [] if include_invites else ["m.room.message"],
                },
                "ephemeral": {"types": []},
                "account_data": {"types": ["m.marked_unread"]},
            },
        }
        params: dict[str, str | int] = {
            "timeout": 0,
            "set_presence": "offline",
            "filter": json.dumps(sync_filter),
        }
        result = await self.http.json("GET", "/_matrix/client/v3/sync", params=params)
        try:
            return _Sync.model_validate(result, strict=True)
        except ValidationError:
            msg = "Matrix returned an invalid sync response"
            raise RuntimeError(msg) from None


def _identifier(value: str, *, sigils: str) -> str:
    if "!" in sigils and re.fullmatch(r"![A-Za-z0-9_-]+", value):
        return quote(value, safe="")
    if not re.fullmatch(rf"[{re.escape(sigils)}][^\s:]+:[^\s/?#@]+", value):
        msg = "Invalid Matrix room, alias, or user ID"
        raise ValueError(msg)
    return quote(value, safe="")


def _room_result(result: dict[str, Any]) -> str:
    room_id = result.get("room_id")
    if not isinstance(room_id, str):
        msg = "Matrix returned an invalid room ID"
        raise RuntimeError(msg)  # noqa: TRY004 - Invalid upstream response, not caller input.
    _identifier(room_id, sigils="!")
    return room_id


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= _MAX_PAGE or offset < 0:
        msg = "limit must be between 1 and 100 and offset must be nonnegative"
        raise ValueError(msg)


def _next_offset(total: int, offset: int, limit: int) -> int | None:
    end = offset + limit
    return end if end < total else None


def _content(event: dict[str, Any]) -> dict[str, Any]:
    content = event.get("content")
    return cast("dict[str, Any]", content) if isinstance(content, dict) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _state_value(events: list[dict[str, Any]], kind: str, key: str) -> str | None:
    return next(
        (
            _string(_content(event).get(key))
            for event in reversed(events)
            if event.get("type") == kind
        ),
        None,
    )


def _mentions(events: list[dict[str, Any]], user_id: str) -> list[MentionEvent]:
    mentions = []
    for event in events:
        content = _content(event)
        mention = content.get("m.mentions")
        if event.get("type") != "m.room.message" or not isinstance(mention, dict):
            continue
        users = mention.get("user_ids")
        if mention.get("room") is not True and not (isinstance(users, list) and user_id in users):
            continue
        if isinstance(event.get("event_id"), str) and isinstance(event.get("sender"), str):
            mentions.append(
                MentionEvent(
                    event_id=event["event_id"],
                    sender=event["sender"],
                    body=_string(content.get("body")),
                    timestamp_ms=event.get("origin_server_ts"),
                )
            )
    return mentions
