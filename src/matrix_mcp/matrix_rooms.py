from __future__ import annotations

import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError

from matrix_mcp.matrix_http import MatrixHTTPError, quote_matrix_id

if TYPE_CHECKING:
    from matrix_mcp.e2ee import RoomCrypto
    from matrix_mcp.matrix_http import MatrixHTTP


_MAX_PAGE = 100
_MAX_INVITEES = 100
_MAX_HIERARCHY_DEPTH = 5
_MAX_RECEIPT_EVENT_FETCHES = 20
_MEGOLM_ALGORITHM = "m.megolm.v1.aes-sha2"
_ACTIVITY_TYPES = ["m.room.message", "m.room.encrypted"]
# Some homeservers (Tuwunel) apply the type filter after taking the newest events,
# so look past a few trailing state changes to find the newest message.
_ACTIVITY_WINDOW = 10
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
# Unread timelines keep Megolm fields so encrypted mentions can be decrypted.
_MEGOLM_EVENT_FIELDS = [
    "content.algorithm",
    "content.ciphertext",
    "content.sender_key",
    "content.session_id",
    "content.device_id",
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


class DirectRoom(BaseModel):
    room_id: str
    created: bool = Field(
        description="False when an existing direct chat with this user was reused."
    )


class SpaceRoom(BaseModel):
    room_id: str
    name: str | None = None
    topic: str | None = None
    canonical_alias: str | None = None
    room_type: str | None = None
    joined_member_count: int | None = None
    join_rule: str | None = None
    joined: bool = Field(description="Whether the connected user has joined this room.")
    children: list[str] = Field(default_factory=list)


class SpaceHierarchy(BaseModel):
    rooms: list[SpaceRoom]
    next_batch: str | None = None
    skipped: int = Field(
        default=0, description="Rooms left out because the homeserver described them invalidly."
    )


class ReadReceipt(BaseModel):
    user_id: str
    event_id: str
    timestamp_ms: int | None = None
    thread_id: str | None = None
    private: bool = False
    read: bool | None = Field(
        default=None,
        description=(
            "With event_id: whether this receipt is at or after that event by server "
            "timestamps; null when not checked."
        ),
    )


class ReceiptList(BaseModel):
    receipts: list[ReadReceipt]
    total: int


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
    ephemeral: _EventBatch = Field(default_factory=_EventBatch)
    timeline: _Timeline = Field(default_factory=_Timeline)
    unread_notifications: _Counts = Field(default_factory=_Counts)


class _Rooms(BaseModel):
    join: dict[str, _RoomData] = Field(default_factory=dict)
    invite: dict[str, _RoomData] = Field(default_factory=dict)


class _Sync(BaseModel):
    next_batch: str
    rooms: _Rooms = Field(default_factory=_Rooms)


class _HierarchyChild(BaseModel):
    type: str
    state_key: str
    content: dict[str, Any] = Field(default_factory=dict)


class _HierarchyRoom(BaseModel):
    room_id: str
    name: str | None = None
    topic: str | None = None
    canonical_alias: str | None = None
    room_type: str | None = None
    num_joined_members: int | None = None
    join_rule: str | None = None
    children_state: list[_HierarchyChild] = Field(default_factory=list)


class _Hierarchy(BaseModel):
    rooms: list[dict[str, Any]]
    next_batch: str | None = None


class _JoinedRooms(BaseModel):
    joined_rooms: list[str]


class MatrixRooms:
    def __init__(self, http: MatrixHTTP, crypto: RoomCrypto | None = None) -> None:
        self.http = http
        self.crypto = crypto

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

    async def create_dm(self, user_id: str, *, encrypted: bool = False) -> DirectRoom:
        _identifier(user_id, sigils="@")
        if user_id == self.http.user_id:
            msg = "Cannot start a direct chat with the connected user"
            raise ValueError(msg)
        if encrypted and self.crypto is None:
            msg = "Encrypted direct chats need local (stdio) mode with end-to-end encryption"
            raise ValueError(msg)
        direct = await self._direct_chats()
        joined = set(await self._joined_rooms())
        for listed_room in reversed(_string_list(direct.get(user_id))):
            if listed_room in joined and await self._in_room(listed_room, user_id):
                return DirectRoom(room_id=listed_room, created=False)
        body: dict[str, object] = {
            "visibility": "private",
            "preset": "trusted_private_chat",
            "is_direct": True,
            "invite": [user_id],
        }
        if encrypted:
            body["initial_state"] = [
                {
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": _MEGOLM_ALGORITHM},
                }
            ]
        room_id = _room_result(
            await self.http.json("POST", "/_matrix/client/v3/createRoom", body=body)
        )
        try:
            # Re-read right before writing: room creation can take seconds, and other
            # clients' changes to the map must survive. Only this user's list gains the room.
            direct = await self._direct_chats()
            listed = _string_list(direct.get(user_id))
            updated = {**direct, user_id: [*listed, room_id]}
            await self.http.json("PUT", self._direct_chats_path(), body=updated)
        except RuntimeError:
            msg = (
                f"Created direct chat {room_id}, but the direct-chat marker was not saved; "
                "other clients may not list it as a direct chat"
            )
            raise RuntimeError(msg) from None
        return DirectRoom(room_id=room_id, created=True)

    async def hierarchy(
        self,
        space_id: str,
        *,
        limit: int = 50,
        max_depth: int = 1,
        next_batch: str | None = None,
    ) -> SpaceHierarchy:
        space = _identifier(space_id, sigils="!")
        _validate_page(limit, 0)
        if not 1 <= max_depth <= _MAX_HIERARCHY_DEPTH:
            msg = "max_depth must be between 1 and 5"
            raise ValueError(msg)
        params: dict[str, str | int] = {"limit": limit, "max_depth": max_depth}
        if next_batch is not None:
            if not next_batch:
                msg = "Matrix hierarchy cursor must not be empty"
                raise ValueError(msg)
            params["from"] = next_batch
        result = await self.http.json(
            "GET", f"/_matrix/client/v1/rooms/{space}/hierarchy", params=params
        )
        try:
            hierarchy = _Hierarchy.model_validate(result, strict=True)
        except ValidationError:
            msg = "Matrix returned an invalid space hierarchy"
            raise RuntimeError(msg) from None
        # Rooms can come from other servers; one malformed summary must not hide the rest.
        valid: list[_HierarchyRoom] = []
        for raw in hierarchy.rooms:
            try:
                valid.append(_HierarchyRoom.model_validate(raw, strict=True))
            except ValidationError:
                continue
        joined = set(await self._joined_rooms())
        return SpaceHierarchy(
            rooms=[
                SpaceRoom(
                    room_id=room.room_id,
                    name=room.name,
                    topic=room.topic,
                    canonical_alias=room.canonical_alias,
                    room_type=room.room_type,
                    joined_member_count=room.num_joined_members,
                    join_rule=room.join_rule,
                    joined=room.room_id in joined,
                    children=[
                        child.state_key
                        for child in room.children_state
                        if child.type == "m.space.child" and _string_list(child.content.get("via"))
                    ],
                )
                for room in valid
            ],
            next_batch=hierarchy.next_batch or None,
            skipped=len(hierarchy.rooms) - len(valid),
        )

    async def receipts(
        self,
        room_id: str,
        *,
        event_id: str | None = None,
        limit: int = 50,
    ) -> ReceiptList:
        room = _identifier(room_id, sigils="!")
        _validate_page(limit, 0)
        if event_id is not None:
            quote_matrix_id(event_id, sigil="$", label="event ID")
        snapshot = await self._filtered_sync(
            {
                "presence": {"types": []},
                "account_data": {"types": []},
                "room": {
                    "rooms": [room_id],
                    "state": {"types": []},
                    "timeline": {"limit": 1},
                    "ephemeral": {"types": ["m.receipt"]},
                    "account_data": {"types": []},
                },
            }
        )
        joined = snapshot.rooms.join.get(room_id)
        receipts = _newest_receipts(
            joined.ephemeral.events if joined is not None else [], self.http.user_id
        )
        page = receipts[:limit]
        if event_id is not None:
            page = await self._mark_read_state(room, event_id, page)
        return ReceiptList(receipts=page, total=len(receipts))

    async def latest_activity(self) -> dict[str, int]:
        snapshot = await self._filtered_sync(
            {
                "event_fields": ["origin_server_ts", "type"],
                "presence": {"types": []},
                "account_data": {"types": []},
                "room": {
                    "state": {"types": []},
                    "timeline": {"limit": _ACTIVITY_WINDOW, "types": _ACTIVITY_TYPES},
                    "ephemeral": {"types": []},
                    "account_data": {"types": []},
                },
            }
        )
        activity = {}
        for room_id, room in snapshot.rooms.join.items():
            timestamps = [
                timestamp
                for event in room.timeline.events
                if (timestamp := _timestamp(event.get("origin_server_ts"))) is not None
            ]
            if timestamps:
                activity[room_id] = max(timestamps)
        return activity

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
                    mentions=_mentions(
                        await self._readable(room_id, room.timeline.events[:timeline_limit]),
                        self.http.user_id,
                    ),
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

    async def _mark_read_state(
        self, room: str, event_id: str, receipts: list[ReadReceipt]
    ) -> list[ReadReceipt]:
        target, target_thread = await self._event_position(room, event_id)
        # A threaded receipt covers only its own thread; unthreaded receipts cover all.
        applicable = [r for r in receipts if r.thread_id in {None, target_thread}]
        # Receipts usually cluster on a few recent events, so a small fetch cap covers most.
        candidates = list(dict.fromkeys(r.event_id for r in applicable if r.event_id != event_id))
        timestamps: dict[str, int | None] = {}
        for candidate in candidates[:_MAX_RECEIPT_EVENT_FETCHES]:
            try:
                timestamps[candidate], _ = await self._event_position(room, candidate)
            except MatrixHTTPError as exc:
                if exc.status_code not in {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}:
                    raise
                timestamps[candidate] = None
        marked = []
        for receipt in receipts:
            read: bool | None = None
            if receipt.thread_id in {None, target_thread}:
                if receipt.event_id == event_id:
                    read = True
                elif (timestamp := timestamps.get(receipt.event_id)) is not None:
                    read = timestamp >= target
            marked.append(receipt.model_copy(update={"read": read}))
        return marked

    async def _event_position(self, room: str, event_id: str) -> tuple[int, str]:
        """Return an event's server timestamp and receipt thread ("main" outside threads)."""
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        result = await self.http.json("GET", f"/_matrix/client/v3/rooms/{room}/event/{event}")
        timestamp = _timestamp(result.get("origin_server_ts"))
        if result.get("event_id") != event_id or timestamp is None:
            msg = "Matrix returned an invalid event for the read receipt check"
            raise RuntimeError(msg)
        relation = _content(result).get("m.relates_to")
        thread = (
            relation.get("event_id")
            if isinstance(relation, dict) and relation.get("rel_type") == "m.thread"
            else None
        )
        return timestamp, thread if isinstance(thread, str) else "main"

    async def _in_room(self, room_id: str, user_id: str) -> bool:
        member = await self.http.room_state(room_id, "m.room.member", user_id)
        return member is not None and member.get("membership") in {"join", "invite"}

    def _direct_chats_path(self) -> str:
        user = _identifier(self.http.user_id, sigils="@")
        return f"/_matrix/client/v3/user/{user}/account_data/m.direct"

    async def _direct_chats(self) -> dict[str, Any]:
        try:
            return await self.http.json("GET", self._direct_chats_path())
        except MatrixHTTPError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND and exc.errcode == "M_NOT_FOUND":
                return {}
            raise

    async def _joined_rooms(self) -> list[str]:
        result = await self.http.json("GET", "/_matrix/client/v3/joined_rooms")
        try:
            return _JoinedRooms.model_validate(result, strict=True).joined_rooms
        except ValidationError:
            msg = "Matrix returned an invalid joined rooms response"
            raise RuntimeError(msg) from None

    async def _readable(self, room_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.crypto is None:
            return events
        readable = []
        for event in events:
            if event.get("type") == "m.room.encrypted":
                event = (await self.crypto.decrypt(room_id, event)).event  # noqa: PLW2901
            readable.append(event)
        return readable

    async def _sync(
        self,
        *,
        timeline_limit: int,
        include_invites: bool = False,
    ) -> _Sync:
        sync_filter: dict[str, object] = {
            "event_fields": _SYNC_EVENT_FIELDS
            if include_invites
            else [*_SYNC_EVENT_FIELDS, *_MEGOLM_EVENT_FIELDS],
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
                    "types": [] if include_invites else ["m.room.message", "m.room.encrypted"],
                },
                "ephemeral": {"types": []},
                "account_data": {"types": ["m.marked_unread"]},
            },
        }
        return await self._filtered_sync(sync_filter)

    async def _filtered_sync(self, sync_filter: dict[str, object]) -> _Sync:
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


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return cast("list[str]", value)


def _timestamp(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _newest_receipts(events: list[dict[str, Any]], user_id: str) -> list[ReadReceipt]:
    newest: dict[tuple[str, str | None, bool], ReadReceipt] = {}
    for event in events:
        if event.get("type") != "m.receipt":
            continue
        for event_id, by_type in _content(event).items():
            if not isinstance(by_type, dict) or not event_id.startswith("$"):
                continue
            for receipt_type in ("m.read", "m.read.private"):
                users = by_type.get(receipt_type)
                if not isinstance(users, dict):
                    continue
                private = receipt_type == "m.read.private"
                for reader, data in users.items():
                    # Private receipts are visible only to their owner.
                    if (
                        not isinstance(data, dict)
                        or not reader.startswith("@")
                        or (private and reader != user_id)
                    ):
                        continue
                    receipt = ReadReceipt(
                        user_id=reader,
                        event_id=event_id,
                        timestamp_ms=_timestamp(data.get("ts")),
                        thread_id=_string(data.get("thread_id")),
                        private=private,
                    )
                    key = (reader, receipt.thread_id, private)
                    current = newest.get(key)
                    if current is None or (receipt.timestamp_ms or -1) > (
                        current.timestamp_ms or -1
                    ):
                        newest[key] = receipt
    return sorted(
        newest.values(),
        key=lambda receipt: (
            receipt.timestamp_ms is None,
            -(receipt.timestamp_ms or 0),
            receipt.user_id,
            receipt.event_id,
        ),
    )


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
