"""Room moderation, power levels, and pinned messages."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from matrix_mcp.matrix_http import MatrixHTTPError, quote_matrix_id

if TYPE_CHECKING:
    from matrix_mcp.matrix_http import MatrixHTTP


_POWER_LEVELS = "m.room.power_levels"
_PINNED_EVENTS = "m.room.pinned_events"
# Canonical JSON integers; older room versions may still store levels as strings.
_MAX_LEVEL = 2**53 - 1
_LEVEL_STRING = re.compile(r"[+-]?[0-9]+")
_USER_ID = re.compile(r"@[^\s:]+:[^\s/?#@]+")
# From room version 12, creators outrank every power level and are not listed in it.
_FIRST_CREATOR_VERSION = 12


class PowerLevels(BaseModel):
    creators: list[str] = Field(
        default_factory=list,
        description=(
            "Room creators (room version 12+): they outrank every power level and are not "
            "listed in users."
        ),
    )
    users: dict[str, int] = Field(default_factory=dict)
    users_default: int = 0
    events: dict[str, int] = Field(default_factory=dict)
    events_default: int = 0
    state_default: int = 50
    invite: int = 0
    kick: int = 50
    ban: int = 50
    redact: int = 50
    own_level: int | None = Field(
        description=(
            "The connected user's power level in this room; null when they are a room "
            "creator, which outranks every level, or when the room's creator is unknown."
        )
    )


class PinnedEvents(BaseModel):
    pinned: list[str]
    changed: bool = Field(description="False when the pin list already matched the request.")


class MatrixModeration:
    def __init__(self, http: MatrixHTTP) -> None:
        self.http = http

    async def kick(self, room_id: str, user_id: str, *, reason: str | None = None) -> None:
        await self._membership_action("kick", room_id, user_id, reason=reason)

    async def ban(self, room_id: str, user_id: str, *, reason: str | None = None) -> None:
        await self._membership_action("ban", room_id, user_id, reason=reason)

    async def unban(self, room_id: str, user_id: str, *, reason: str | None = None) -> None:
        await self._membership_action("unban", room_id, user_id, reason=reason)

    async def power_levels(self, room_id: str) -> PowerLevels:
        stored = await self.http.room_state(room_id, _POWER_LEVELS)
        create, sender = await self._create_event(room_id)
        creators = _creators(create, sender)
        content = stored or {}
        defaults = PowerLevels(own_level=0)
        users = _level_map(content.get("users"))
        unknown_creator = False
        if stored is None and not creators:
            # Without a power levels event, the room creator alone has level 100.
            creator = create.get("creator", sender)
            if isinstance(creator, str):
                users = {creator: 100}
            else:
                unknown_creator = True
        users_default = _level(content.get("users_default"), defaults.users_default)
        # state_default is 50 when unspecified, but 0 when there is no power levels event.
        state_default = defaults.state_default if stored is not None else 0
        return PowerLevels(
            creators=creators,
            users=users,
            users_default=users_default,
            events=_level_map(content.get("events")),
            events_default=_level(content.get("events_default"), defaults.events_default),
            state_default=_level(content.get("state_default"), state_default),
            invite=_level(content.get("invite"), defaults.invite),
            kick=_level(content.get("kick"), defaults.kick),
            ban=_level(content.get("ban"), defaults.ban),
            redact=_level(content.get("redact"), defaults.redact),
            own_level=None
            if self.http.user_id in creators or unknown_creator
            else users.get(self.http.user_id, users_default),
        )

    async def set_power_level(self, room_id: str, user_id: str, level: int | None) -> str:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        _validate_user_id(user_id)
        if level is not None and (
            not isinstance(level, int) or isinstance(level, bool) or abs(level) > _MAX_LEVEL
        ):
            msg = f"level must be an integer between {-_MAX_LEVEL} and {_MAX_LEVEL}"
            raise ValueError(msg)
        # Overlapping calls would each write back a users map missing the other's change.
        async with self.http.write_lock(room_id, _POWER_LEVELS):
            content = await self.http.room_state(room_id, _POWER_LEVELS)
            if content is None:
                msg = "Matrix room has no power levels event to update"
                raise RuntimeError(msg)
            if user_id in _creators(*await self._create_event(room_id)):
                msg = "Room creators outrank every power level; their level cannot be set"
                raise ValueError(msg)
            users = content.get("users")
            users = dict(users) if isinstance(users, dict) else {}
            if user_id == self.http.user_id:
                users_default = _level(content.get("users_default"), 0)
                current = _level(users.get(user_id), users_default)
                new = users_default if level is None else level
                if new < current:
                    msg = (
                        "Refusing to lower your own power level: it cannot be undone without "
                        "someone else's help"
                    )
                    raise ValueError(msg)
            if level is None:
                users.pop(user_id, None)
            else:
                users[user_id] = level
            payload = await self.http.json(
                "PUT",
                f"/_matrix/client/v3/rooms/{room}/state/{_POWER_LEVELS}",
                body={**content, "users": users},
            )
            return _event_id(payload)

    async def pin(self, room_id: str, event_id: str) -> PinnedEvents:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        # Only pin events that exist in this room.
        await self.http.json("GET", f"/_matrix/client/v3/rooms/{room}/event/{event}")
        async with self.http.write_lock(room_id, _PINNED_EVENTS):
            content, pinned = await self._pinned(room_id)
            if event_id in pinned:
                return PinnedEvents(pinned=pinned, changed=False)
            pinned = [*pinned, event_id]
            await self._put_pinned(room, content, pinned)
        return PinnedEvents(pinned=pinned, changed=True)

    async def unpin(self, room_id: str, event_id: str) -> PinnedEvents:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        quote_matrix_id(event_id, sigil="$", label="event ID")
        async with self.http.write_lock(room_id, _PINNED_EVENTS):
            content, pinned = await self._pinned(room_id)
            if event_id not in pinned:
                return PinnedEvents(pinned=pinned, changed=False)
            pinned = [pinned_id for pinned_id in pinned if pinned_id != event_id]
            await self._put_pinned(room, content, pinned)
        return PinnedEvents(pinned=pinned, changed=True)

    async def _membership_action(
        self, action: str, room_id: str, user_id: str, *, reason: str | None
    ) -> None:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        _validate_user_id(user_id)
        if action != "unban" and user_id == self.http.user_id:
            msg = f"Cannot {action} the connected user; use matrix_leave_room to leave"
            raise ValueError(msg)
        body: dict[str, object] = {"user_id": user_id}
        if reason is not None:
            body["reason"] = reason
        await self.http.json("POST", f"/_matrix/client/v3/rooms/{room}/{action}", body=body)

    async def _create_event(self, room_id: str) -> tuple[dict[str, Any], str | None]:
        """Return the room's create event content and its sender, when known."""
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        path = f"/_matrix/client/v3/rooms/{room}/state/m.room.create"
        try:
            payload = await self.http.json("GET", path, params={"format": "event"})
        except MatrixHTTPError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND and exc.errcode == "M_NOT_FOUND":
                return {}, None
            raise
        # Servers without the format parameter return only the content.
        wrapped = isinstance(payload.get("content"), dict) and isinstance(
            payload.get("sender"), str
        )
        content: dict[str, Any] = payload["content"] if wrapped else payload
        sender = payload["sender"] if wrapped else None
        if sender is None and _creators_outrank_levels(content.get("room_version")):
            # These room versions derive the room ID from the create event's ID.
            create_id = quote_matrix_id(f"${room_id[1:]}", sigil="$", label="event ID")
            event = await self.http.json(
                "GET", f"/_matrix/client/v3/rooms/{room}/event/{create_id}"
            )
            sender = event.get("sender") if event.get("type") == "m.room.create" else None
        return content, sender if isinstance(sender, str) else None

    async def _pinned(self, room_id: str) -> tuple[dict[str, Any], list[str]]:
        content = await self.http.room_state(room_id, _PINNED_EVENTS) or {}
        pinned = content.get("pinned")
        if not isinstance(pinned, list):
            return content, []
        return content, [event_id for event_id in pinned if isinstance(event_id, str)]

    async def _put_pinned(self, room: str, content: dict[str, Any], pinned: list[str]) -> None:
        await self.http.json(
            "PUT",
            f"/_matrix/client/v3/rooms/{room}/state/{_PINNED_EVENTS}",
            body={**content, "pinned": pinned},
        )


def _validate_user_id(user_id: str) -> None:
    quote_matrix_id(user_id, sigil="@", label="user ID")
    if _USER_ID.fullmatch(user_id) is None:
        msg = "Invalid Matrix user ID"
        raise ValueError(msg)


def _creators(create: dict[str, Any], sender: str | None) -> list[str]:
    if not _creators_outrank_levels(create.get("room_version")):
        return []
    additional = create.get("additional_creators")
    creators = [sender] if sender is not None else []
    if isinstance(additional, list):
        creators.extend(user for user in additional if isinstance(user, str))
    return list(dict.fromkeys(creators))


def _creators_outrank_levels(room_version: object) -> bool:
    if not isinstance(room_version, str):
        return False
    if room_version.isdigit():
        return int(room_version) >= _FIRST_CREATOR_VERSION
    return room_version.startswith("org.matrix.hydra.")


def _level(value: object, default: int) -> int:
    parsed = _parse_level(value)
    return default if parsed is None else parsed


def _parse_level(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _LEVEL_STRING.fullmatch(value):
        return int(value)
    return None


def _level_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: level
        for key, raw in value.items()
        if isinstance(key, str) and (level := _parse_level(raw)) is not None
    }


def _event_id(payload: dict[str, Any]) -> str:
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        msg = "Matrix state response did not include an event ID"
        raise RuntimeError(msg)
    return event_id
