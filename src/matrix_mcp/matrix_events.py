from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from matrix_mcp.matrix_http import MatrixHTTP, quote_matrix_id, quote_transaction_id

_MAX_HISTORY_LIMIT = 100
_MAX_CONTEXT_LIMIT = 50
_RELATION_PAGE_LIMIT = 50
_MAX_RELATION_PAGES = 2
_EDITABLE_MSGTYPES = frozenset({"m.text", "m.notice", "m.emote"})
_MEDIA_MSGTYPES = frozenset({"m.file", "m.image", "m.video", "m.audio"})


class MediaMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    filename: str | None = None
    mimetype: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


class TimelineEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    sender: str
    timestamp_ms: int | None = None
    type: str
    msgtype: str | None = None
    body: str | None = None
    thread_id: str | None = None
    reply_to: str | None = None
    media: MediaMetadata | None = None
    edited: bool = False
    redacted: bool = False


class HistoryPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    events: list[TimelineEvent]
    next_batch: str | None = None
    edit_resolution_truncated: bool = False


class EventContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: TimelineEvent
    events_before: list[TimelineEvent]
    events_after: list[TimelineEvent]
    start: str | None = None
    end: str | None = None
    edit_resolution_truncated: bool = False


class MatrixEvents:
    def __init__(self, http: MatrixHTTP) -> None:
        self.http = http

    async def history(
        self,
        room_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
    ) -> HistoryPage:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        page_limit = _bounded_limit(limit, maximum=_MAX_HISTORY_LIMIT)
        params: dict[str, str | int] = {"dir": "b", "limit": page_limit}
        if before is not None:
            if not isinstance(before, str) or not before:
                msg = "Matrix history cursor must not be empty"
                raise ValueError(msg)
            params["from"] = before
        payload = await self.http.json(
            "GET",
            f"/_matrix/client/v3/rooms/{room}/messages",
            params=params,
        )
        raw_events = _event_list(payload, "chunk", required=True)
        _require_page_bound(raw_events, page_limit)
        visible = [raw for raw in raw_events if not _is_replacement(raw)]
        expanded, truncated = await self._expand_many(room_id, visible)
        return HistoryPage(
            events=expanded,
            next_batch=_optional_string_field(payload, "end"),
            edit_resolution_truncated=truncated,
        )

    async def context(
        self,
        room_id: str,
        event_id: str,
        *,
        limit: int = 10,
    ) -> EventContext:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        context_limit = _bounded_limit(limit, maximum=_MAX_CONTEXT_LIMIT)
        payload = await self.http.json(
            "GET",
            f"/_matrix/client/v3/rooms/{room}/context/{event}",
            params={"limit": context_limit},
        )
        raw_event = _mapping_field(payload, "event", required=True)
        raw_before = _event_list(payload, "events_before")
        raw_after = _event_list(payload, "events_after")
        _require_page_bound(raw_before + raw_after, context_limit)
        before = [raw for raw in raw_before if not _is_replacement(raw)]
        after = [raw for raw in raw_after if not _is_replacement(raw)]
        center, center_truncated = await self._expand_one(room_id, raw_event)
        events_before, before_truncated = await self._expand_many(room_id, before)
        events_after, after_truncated = await self._expand_many(room_id, after)
        return EventContext(
            event=center,
            events_before=events_before,
            events_after=events_after,
            start=_optional_string_field(payload, "start"),
            end=_optional_string_field(payload, "end"),
            edit_resolution_truncated=center_truncated or before_truncated or after_truncated,
        )

    async def reply(
        self,
        room_id: str,
        event_id: str,
        body: str,
        *,
        transaction_id: str | None = None,
    ) -> str:
        quote_matrix_id(room_id, sigil="!", label="room ID")
        quote_matrix_id(event_id, sigil="$", label="event ID")
        transaction = _transaction_path(transaction_id)
        _validate_body(body)
        target = await self._fetch_event(room_id, event_id)
        await self.http.require_unencrypted(room_id)
        relation: dict[str, object] = {"m.in_reply_to": {"event_id": event_id}}
        thread_id = _relationship_id(target, "m.thread")
        if thread_id is not None:
            relation.update(
                {
                    "rel_type": "m.thread",
                    "event_id": thread_id,
                    "is_falling_back": False,
                }
            )
        return await self._send(
            room_id,
            "m.room.message",
            {"msgtype": "m.text", "body": body, "m.relates_to": relation},
            transaction,
        )

    async def react(
        self,
        room_id: str,
        event_id: str,
        key: str,
        *,
        transaction_id: str | None = None,
    ) -> str:
        quote_matrix_id(room_id, sigil="!", label="room ID")
        quote_matrix_id(event_id, sigil="$", label="event ID")
        transaction = _transaction_path(transaction_id)
        if not key or not key.strip():
            msg = "Reaction key must not be empty"
            raise ValueError(msg)
        await self.http.require_unencrypted(room_id)
        return await self._send(
            room_id,
            "m.reaction",
            {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": key,
                }
            },
            transaction,
        )

    async def edit(
        self,
        room_id: str,
        event_id: str,
        body: str,
        *,
        transaction_id: str | None = None,
    ) -> str:
        quote_matrix_id(room_id, sigil="!", label="room ID")
        quote_matrix_id(event_id, sigil="$", label="event ID")
        transaction = _transaction_path(transaction_id)
        _validate_body(body)
        target = await self._fetch_event(room_id, event_id)
        _require_own_event(target, self.http.user_id)
        if _is_redacted(target):
            msg = "Cannot edit a redacted Matrix event"
            raise ValueError(msg)
        if "state_key" in target or _is_replacement(target):
            msg = "Matrix event is not a valid replacement target"
            raise ValueError(msg)
        if _required_string(target, "type", context="Matrix event") != "m.room.message":
            msg = "Only Matrix text, notice, or emote messages can be edited"
            raise ValueError(msg)
        content = _mapping_field(target, "content", required=True)
        msgtype = content.get("msgtype")
        if msgtype not in _EDITABLE_MSGTYPES:
            msg = "Only Matrix text, notice, or emote messages can be edited"
            raise ValueError(msg)
        await self.http.require_unencrypted(room_id)
        return await self._send(
            room_id,
            "m.room.message",
            {
                "msgtype": msgtype,
                "body": f"* {body}",
                "m.new_content": {"msgtype": msgtype, "body": body},
                "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
            },
            transaction,
        )

    async def redact(
        self,
        room_id: str,
        event_id: str,
        *,
        reason: str | None = None,
        transaction_id: str | None = None,
    ) -> str:
        quote_matrix_id(room_id, sigil="!", label="room ID")
        quote_matrix_id(event_id, sigil="$", label="event ID")
        transaction = _transaction_path(transaction_id)
        target = await self._fetch_event(room_id, event_id)
        _require_own_event(target, self.http.user_id)
        content: dict[str, object] = {}
        if reason is not None:
            content["reason"] = reason
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        payload = await self.http.json(
            "PUT",
            f"/_matrix/client/v3/rooms/{room}/redact/{event}/{transaction}",
            body=content,
        )
        return _required_string(payload, "event_id", context="Matrix redact response")

    async def _fetch_event(self, room_id: str, event_id: str) -> dict[str, Any]:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        payload = await self.http.json(
            "GET",
            f"/_matrix/client/v3/rooms/{room}/event/{event}",
        )
        actual_event_id = _required_string(payload, "event_id", context="Matrix event")
        if actual_event_id != event_id:
            msg = "Matrix event response did not match the requested event"
            raise RuntimeError(msg)
        _required_string(payload, "sender", context="Matrix event")
        _required_string(payload, "type", context="Matrix event")
        _mapping_field(payload, "content", required=True)
        return payload

    async def _send(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, object],
        transaction: str,
    ) -> str:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        payload = await self.http.json(
            "PUT",
            f"/_matrix/client/v3/rooms/{room}/send/{event_type}/{transaction}",
            body=content,
        )
        return _required_string(payload, "event_id", context="Matrix send response")

    async def _expand_many(
        self, room_id: str, raw_events: list[dict[str, Any]]
    ) -> tuple[list[TimelineEvent], bool]:
        events: list[TimelineEvent] = []
        truncated = False
        for raw in raw_events:
            event, event_truncated = await self._expand_one(room_id, raw)
            events.append(event)
            truncated = truncated or event_truncated
        return events, truncated

    async def _expand_one(self, room_id: str, raw: dict[str, Any]) -> tuple[TimelineEvent, bool]:
        original = normalize_timeline_event(room_id, raw)
        if original.redacted or _is_replacement(raw) or "state_key" in raw:
            return original, False
        _, bundle_present = _bundled_replacement(raw)
        if original.edited:
            return original, False
        if not bundle_present:
            return original, False
        replacements, truncated = await self._replacement_relations(room_id, original.event_id)
        valid = [
            replacement
            for replacement in replacements
            if _valid_replacement(room_id, raw, replacement)
        ]
        if not valid:
            return original, truncated
        latest = max(valid, key=_replacement_order)
        return _apply_replacement(original, latest), truncated

    async def _replacement_relations(
        self, room_id: str, event_id: str
    ) -> tuple[list[dict[str, Any]], bool]:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        path = f"/_matrix/client/v1/rooms/{room}/relations/{event}/m.replace/m.room.message"
        replacements: list[dict[str, Any]] = []
        cursor: str | None = None
        for page_number in range(_MAX_RELATION_PAGES):
            params: dict[str, str | int] = {"dir": "b", "limit": _RELATION_PAGE_LIMIT}
            if cursor is not None:
                params["from"] = cursor
            payload = await self.http.json("GET", path, params=params)
            chunk = _event_list(payload, "chunk", required=True)
            _require_page_bound(chunk, _RELATION_PAGE_LIMIT)
            replacements.extend(chunk)
            cursor = _optional_string_field(payload, "next_batch")
            if cursor is None:
                return replacements, False
            if page_number + 1 == _MAX_RELATION_PAGES:
                return replacements, True
        return replacements, cursor is not None


def normalize_timeline_event(
    room_id: str,
    raw: dict[str, Any],
    *,
    replacement: dict[str, Any] | None = None,
) -> TimelineEvent:
    """Normalize a raw Matrix event and apply one valid replacement when available."""
    original = _timeline_event(raw)
    if original.redacted or _is_replacement(raw) or "state_key" in raw:
        return original
    candidate = replacement
    if candidate is None:
        candidate, _ = _bundled_replacement(raw)
    if candidate is not None and _valid_replacement(room_id, raw, candidate):
        return _apply_replacement(original, candidate)
    return original


def _timeline_event(raw: dict[str, Any]) -> TimelineEvent:
    event_id = _required_string(raw, "event_id", context="Matrix event")
    sender = _required_string(raw, "sender", context="Matrix event")
    event_type = _required_string(raw, "type", context="Matrix event")
    redacted = _is_redacted(raw)
    content = _mapping_field(raw, "content", required=True)
    msgtype = _optional_string(content.get("msgtype"))
    body = None if redacted else _optional_string(content.get("body"))
    relation_value = content.get("m.relates_to")
    relation = relation_value if isinstance(relation_value, dict) else {}
    timestamp = raw.get("origin_server_ts")
    if timestamp is not None and (not isinstance(timestamp, int) or isinstance(timestamp, bool)):
        msg = "Matrix event had an invalid origin_server_ts"
        raise RuntimeError(msg)
    return TimelineEvent(
        event_id=event_id,
        sender=sender,
        timestamp_ms=timestamp,
        type=event_type,
        msgtype=msgtype,
        body=body,
        thread_id=_relation_value(relation, "m.thread"),
        reply_to=_nested_event_id(relation, "m.in_reply_to"),
        media=None if redacted else _media_metadata(content, msgtype),
        redacted=redacted,
    )


def _media_metadata(content: dict[str, Any], msgtype: str | None) -> MediaMetadata | None:
    url = _optional_string(content.get("url"))
    if msgtype not in _MEDIA_MSGTYPES or url is None:
        return None
    info_value = content.get("info")
    info = info_value if isinstance(info_value, dict) else {}
    body = _optional_string(content.get("body"))
    return MediaMetadata(
        url=url,
        filename=_optional_string(content.get("filename")) or body,
        mimetype=_optional_string(info.get("mimetype")),
        size=_optional_integer(info.get("size")),
        width=_optional_integer(info.get("w")),
        height=_optional_integer(info.get("h")),
        duration_ms=_optional_integer(info.get("duration")),
    )


def _bundled_replacement(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    unsigned = raw.get("unsigned")
    if not isinstance(unsigned, dict):
        return None, False
    relations = unsigned.get("m.relations")
    if not isinstance(relations, dict) or "m.replace" not in relations:
        return None, False
    replacement = relations.get("m.replace")
    return (replacement if isinstance(replacement, dict) else None), True


def _valid_replacement(room_id: str, original: dict[str, Any], replacement: dict[str, Any]) -> bool:
    if "state_key" in original or "state_key" in replacement or _is_redacted(replacement):
        return False
    content = replacement.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("m.new_content"), dict):
        return False
    relation = content.get("m.relates_to")
    if not isinstance(relation, dict):
        return False
    timestamp = replacement.get("origin_server_ts")
    replacement_room = replacement.get("room_id")
    return (
        replacement.get("sender") == original.get("sender")
        and replacement.get("type") == original.get("type")
        and (replacement_room is None or replacement_room == room_id)
        and relation.get("rel_type") == "m.replace"
        and relation.get("event_id") == original.get("event_id")
        and isinstance(replacement.get("event_id"), str)
        and isinstance(timestamp, int)
        and not isinstance(timestamp, bool)
    )


def _apply_replacement(original: TimelineEvent, replacement: dict[str, Any]) -> TimelineEvent:
    replacement_content = replacement["content"]
    content = replacement_content["m.new_content"]
    msgtype = _optional_string(content.get("msgtype"))
    return original.model_copy(
        update={
            "msgtype": msgtype,
            "body": _optional_string(content.get("body")),
            "media": _media_metadata(content, msgtype),
            "edited": True,
        }
    )


def _replacement_order(raw: dict[str, Any]) -> tuple[int, str]:
    return cast("int", raw["origin_server_ts"]), cast("str", raw["event_id"])


def _is_replacement(raw: dict[str, Any]) -> bool:
    content = raw.get("content")
    if not isinstance(content, dict):
        return False
    relation = content.get("m.relates_to")
    return isinstance(relation, dict) and relation.get("rel_type") == "m.replace"


def _is_redacted(raw: dict[str, Any]) -> bool:
    unsigned = raw.get("unsigned")
    return isinstance(unsigned, dict) and isinstance(unsigned.get("redacted_because"), dict)


def _relationship_id(raw: dict[str, Any], rel_type: str) -> str | None:
    content = raw.get("content")
    if not isinstance(content, dict):
        return None
    relation = content.get("m.relates_to")
    if not isinstance(relation, dict):
        return None
    return _relation_value(relation, rel_type)


def _relation_value(relation: dict[str, Any], rel_type: str) -> str | None:
    if relation.get("rel_type") != rel_type:
        return None
    return _optional_string(relation.get("event_id"))


def _nested_event_id(relation: dict[str, Any], key: str) -> str | None:
    nested = relation.get(key)
    return _optional_string(nested.get("event_id")) if isinstance(nested, dict) else None


def _require_own_event(raw: dict[str, Any], user_id: str) -> None:
    if raw.get("sender") != user_id:
        msg = "Matrix event was not sent by the connected user"
        raise ValueError(msg)


def _transaction_path(transaction_id: str | None) -> str:
    return quote_transaction_id(uuid4().hex if transaction_id is None else transaction_id)


def _bounded_limit(limit: int, *, maximum: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        msg = "limit must be a positive integer"
        raise ValueError(msg)
    return min(limit, maximum)


def _require_page_bound(events: list[dict[str, Any]], limit: int) -> None:
    if len(events) > limit:
        msg = "Matrix response exceeded requested limit"
        raise RuntimeError(msg)


def _validate_body(body: str) -> None:
    if not isinstance(body, str) or not body.strip():
        msg = "Message body must not be empty"
        raise ValueError(msg)


def _event_list(
    payload: dict[str, Any], key: str, *, required: bool = False
) -> list[dict[str, Any]]:
    value = payload.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        msg = f"Matrix response had an invalid {key} field"
        raise RuntimeError(msg)
    return value


def _mapping_field(payload: dict[str, Any], key: str, *, required: bool = False) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    if value is None and not required:
        return {}
    msg = f"Matrix response had an invalid {key} field"
    raise RuntimeError(msg)


def _required_string(payload: dict[str, Any], key: str, *, context: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    msg = f"{context} did not include required string field {key!r}"
    raise RuntimeError(msg)


def _optional_string_field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    msg = f"Matrix response had an invalid {key} field"
    raise RuntimeError(msg)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
