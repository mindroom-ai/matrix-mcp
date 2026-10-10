from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from matrix_mcp.e2ee import E2EE_UNSUPPORTED, DecryptedEvent
from matrix_mcp.matrix_http import MatrixHTTP, quote_matrix_id, quote_transaction_id
from matrix_mcp.matrix_media import EventAttachment

if TYPE_CHECKING:
    from matrix_mcp.e2ee import RoomCrypto

_MAX_HISTORY_LIMIT = 100
_MAX_CONTEXT_LIMIT = 50
_RELATION_PAGE_LIMIT = 50
_MAX_RELATION_PAGES = 2
_MAX_RELATION_REQUESTS = 4
_DECRYPTED = "matrix_mcp.decrypted"
_EDITABLE_MSGTYPES = frozenset({"m.text", "m.notice", "m.emote"})
_MEDIA_MSGTYPES = frozenset({"m.file", "m.image", "m.video", "m.audio"})
_MAX_SEARCH_LIMIT = 50
_MAX_THREAD_LIMIT = 50
_MAX_REACTION_SCAN = 500
_REACTION_PAGE_LIMIT = 100


class MediaMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    filename: str | None = None
    mimetype: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    encrypted: bool = Field(
        default=False,
        description=(
            "End-to-end encrypted attachment; pass room_id and event_id to "
            "matrix_download_media to decrypt it."
        ),
    )


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
    encrypted: bool = Field(
        default=False, description="Whether the event was end-to-end encrypted."
    )
    decryption_error: str | None = Field(
        default=None,
        description=(
            "Why an encrypted event could not be decrypted; its body is then null. "
            "'missing room key': this device never received the message's key."
        ),
    )
    # Relations stay readable on undecryptable events; used to tell reactions apart.
    relation_type: str | None = Field(default=None, exclude=True)


class HistoryPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    events: list[TimelineEvent] = Field(
        description=(
            "Events with edits resolved from valid server bundles and replacements returned "
            "in this page, plus a bounded advertised recovery scan; original content can "
            "remain when a server omits both direct sources."
        )
    )
    next_batch: str | None = None
    edit_resolution_truncated: bool = Field(
        default=False,
        description="Whether a bounded replacement-relation fallback scan was truncated.",
    )


class EventContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: TimelineEvent = Field(
        description=(
            "Requested event with edits resolved from valid server bundles and replacements "
            "returned in this context, plus a bounded advertised recovery scan; original "
            "content can remain when a server omits both direct sources."
        )
    )
    events_before: list[TimelineEvent]
    events_after: list[TimelineEvent]
    start: str | None = None
    end: str | None = None
    edit_resolution_truncated: bool = Field(
        default=False,
        description="Whether a bounded replacement-relation fallback scan was truncated.",
    )


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    room_id: str
    event: TimelineEvent
    rank: float | None = None
    edit_of: str | None = Field(
        default=None,
        description="Original event ID when the match is an edit; its body is the edited text.",
    )


class SearchPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: list[SearchResult]
    count: int | None = Field(
        default=None,
        description="Total matches reported by the homeserver; may be approximate.",
    )
    next_batch: str | None = None


class ThreadSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: TimelineEvent
    reply_count: int | None = None
    latest_reply: TimelineEvent | None = None
    participated: bool | None = Field(
        default=None, description="Whether the connected user has posted in the thread."
    )


class ThreadPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    threads: list[ThreadSummary]
    next_batch: str | None = None
    edit_resolution_truncated: bool = Field(
        default=False,
        description="Whether a bounded replacement-relation fallback scan was truncated.",
    )


class Reaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    count: int
    senders: list[str]


class ReactionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    reactions: list[Reaction]
    unreadable: int = Field(default=0, description="Reaction events whose key could not be read.")
    truncated: bool = Field(
        default=False, description="Whether more reactions may exist beyond the scanned limit."
    )


class MatrixEvents:
    def __init__(self, http: MatrixHTTP, crypto: RoomCrypto | None = None) -> None:
        self.http = http
        self.crypto = crypto

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
        raw_events, encryption = await self.decrypt_raw(room_id, raw_events)
        replacements = _replacement_map(raw_events)
        visible = [raw for raw in raw_events if not _is_replacement(raw)]
        relation_budget = _RelationFetchBudget()
        expanded, truncated = await self._expand_many(
            room_id,
            visible,
            replacements=replacements,
            relation_budget=relation_budget,
        )
        return HistoryPage(
            events=[mark_encryption(event, encryption) for event in expanded],
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
        decrypted, encryption = await self.decrypt_raw(
            room_id, [raw_event, *raw_before, *raw_after]
        )
        raw_event = decrypted[0]
        raw_before = decrypted[1 : 1 + len(raw_before)]
        raw_after = decrypted[1 + len(raw_before) :]
        replacements = _replacement_map(decrypted)
        before = [raw for raw in raw_before if not _is_replacement(raw)]
        after = [raw for raw in raw_after if not _is_replacement(raw)]
        relation_budget = _RelationFetchBudget()
        center, center_truncated = await self._expand_one(
            room_id,
            raw_event,
            replacements=replacements.get(_optional_string(raw_event.get("event_id")) or "", []),
            relation_budget=relation_budget,
        )
        events_before, before_truncated = await self._expand_many(
            room_id,
            before,
            replacements=replacements,
            relation_budget=relation_budget,
        )
        events_after, after_truncated = await self._expand_many(
            room_id,
            after,
            replacements=replacements,
            relation_budget=relation_budget,
        )
        return EventContext(
            event=mark_encryption(center, encryption),
            events_before=[mark_encryption(event, encryption) for event in events_before],
            events_after=[mark_encryption(event, encryption) for event in events_after],
            start=_optional_string_field(payload, "start"),
            end=_optional_string_field(payload, "end"),
            edit_resolution_truncated=center_truncated or before_truncated or after_truncated,
        )

    async def search(
        self,
        search_term: str,
        *,
        room_id: str | None = None,
        limit: int = 10,
        order_by: Literal["recent", "rank"] = "recent",
        next_batch: str | None = None,
    ) -> SearchPage:
        """Search message text with the homeserver's full-text index.

        Homeservers cannot index end-to-end encrypted rooms, so their messages never match.
        """
        if not isinstance(search_term, str) or not search_term.strip():
            msg = "search_term must not be empty"
            raise ValueError(msg)
        page_limit = _page_limit(limit, maximum=_MAX_SEARCH_LIMIT)
        if order_by not in {"recent", "rank"}:
            msg = "order_by must be 'recent' or 'rank'"
            raise ValueError(msg)
        search_filter: dict[str, object] = {"limit": page_limit}
        if room_id is not None:
            quote_matrix_id(room_id, sigil="!", label="room ID")
            search_filter["rooms"] = [room_id]
        params: dict[str, str | int] = {}
        if next_batch is not None:
            params["next_batch"] = _require_cursor(next_batch)
        payload = await self.http.json(
            "POST",
            "/_matrix/client/v3/search",
            body={
                "search_categories": {
                    "room_events": {
                        "search_term": search_term,
                        "order_by": order_by,
                        "filter": search_filter,
                    }
                }
            },
            params=params or None,
        )
        room_events = _mapping_field(_mapping_field(payload, "search_categories"), "room_events")
        hits = _event_list(room_events, "results")
        _require_page_bound(hits, page_limit)
        results = []
        for hit in hits:
            raw = _mapping_field(hit, "result", required=True)
            if _is_redacted(raw):
                continue
            result_room = raw.get("room_id", room_id)
            if not isinstance(result_room, str) or not result_room:
                msg = "Matrix search result did not include a room ID"
                raise RuntimeError(msg)
            event = _timeline_event(raw)
            edit_of = _relationship_id(raw, "m.replace")
            new_content = _mapping_field(raw, "content", required=True).get("m.new_content")
            if edit_of is not None and isinstance(new_content, dict):
                body = _optional_string(new_content.get("body"))
                if body is not None:
                    msgtype = _optional_string(new_content.get("msgtype"))
                    event = event.model_copy(
                        update={
                            "msgtype": msgtype,
                            "body": body,
                            "media": _media_metadata(new_content, msgtype),
                        }
                    )
            rank = hit.get("rank")
            results.append(
                SearchResult(
                    room_id=result_room,
                    event=event,
                    rank=float(rank)
                    if isinstance(rank, int | float) and not isinstance(rank, bool)
                    else None,
                    edit_of=edit_of,
                )
            )
        return SearchPage(
            results=results,
            count=_optional_integer(room_events.get("count")),
            next_batch=_optional_string_field(room_events, "next_batch"),
        )

    async def threads(
        self,
        room_id: str,
        *,
        include: Literal["all", "participated"] = "all",
        limit: int = 20,
        before: str | None = None,
    ) -> ThreadPage:
        """List a room's threads newest first, with reply counts and latest replies."""
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        if include not in {"all", "participated"}:
            msg = "include must be 'all' or 'participated'"
            raise ValueError(msg)
        page_limit = _page_limit(limit, maximum=_MAX_THREAD_LIMIT)
        params: dict[str, str | int] = {"include": include, "limit": page_limit}
        if before is not None:
            params["from"] = _require_cursor(before)
        payload = await self.http.json(
            "GET",
            f"/_matrix/client/v1/rooms/{room}/threads",
            params=params,
        )
        roots = _event_list(payload, "chunk", required=True)
        _require_page_bound(roots, page_limit)
        # Read the server's thread bundle from the raw roots, before decryption.
        bundles = [_thread_bundle(raw) for raw in roots]
        latest_events = [
            latest if isinstance(latest := bundle.get("latest_event"), dict) else None
            for bundle in bundles
        ]
        decrypted, encryption = await self.decrypt_raw(
            room_id, [*roots, *(latest for latest in latest_events if latest is not None)]
        )
        decrypted_roots, decrypted_latest = decrypted[: len(roots)], iter(decrypted[len(roots) :])
        # Resolve edits like history does, so agents never act on text already corrected.
        relation_budget = _RelationFetchBudget()
        threads = []
        truncated = False
        for root, bundle, latest in zip(decrypted_roots, bundles, latest_events, strict=True):
            latest_reply = None
            if latest is not None:
                latest_event, latest_truncated = await self._expand_one(
                    room_id, next(decrypted_latest), relation_budget=relation_budget
                )
                latest_reply = mark_encryption(latest_event, encryption)
                truncated = truncated or latest_truncated
            root_event, root_truncated = await self._expand_one(
                room_id, root, relation_budget=relation_budget
            )
            truncated = truncated or root_truncated
            participated = bundle.get("current_user_participated")
            threads.append(
                ThreadSummary(
                    root=mark_encryption(root_event, encryption),
                    reply_count=_optional_integer(bundle.get("count")),
                    latest_reply=latest_reply,
                    participated=participated if isinstance(participated, bool) else None,
                )
            )
        return ThreadPage(
            threads=threads,
            next_batch=_optional_string_field(payload, "next_batch"),
            edit_resolution_truncated=truncated,
        )

    async def reactions(self, room_id: str, event_id: str, *, limit: int = 200) -> ReactionSummary:
        """Count reactions on an event by key, scanning at most limit reaction events."""
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        scan_limit = _page_limit(limit, maximum=_MAX_REACTION_SCAN)
        # No event type filter: reactions in encrypted rooms are m.room.encrypted.
        path = f"/_matrix/client/v1/rooms/{room}/relations/{event}/m.annotation"
        senders_by_key: dict[str, set[str]] = {}
        unreadable = 0
        scanned = 0
        cursor: str | None = None
        while scanned < scan_limit:
            page_limit = min(_REACTION_PAGE_LIMIT, scan_limit - scanned)
            params: dict[str, str | int] = {"dir": "b", "limit": page_limit}
            if cursor is not None:
                params["from"] = cursor
            payload = await self.http.json("GET", path, params=params)
            chunk = _event_list(payload, "chunk", required=True)
            _require_page_bound(chunk, page_limit)
            decrypted, _ = await self.decrypt_raw(room_id, chunk)
            for raw, readable in zip(chunk, decrypted, strict=True):
                if _is_redacted(raw):
                    continue
                sender = _optional_string(raw.get("sender"))
                relation = _reaction_relation(raw, readable)
                if sender is None or relation is None or relation.get("event_id") != event_id:
                    unreadable += 1
                    continue
                senders_by_key.setdefault(relation["key"], set()).add(sender)
            scanned += len(chunk)
            cursor = _optional_string_field(payload, "next_batch")
            if cursor is None or not chunk:
                break
        reactions = [
            Reaction(key=key, count=len(senders), senders=sorted(senders))
            for key, senders in senders_by_key.items()
        ]
        return ReactionSummary(
            event_id=event_id,
            reactions=sorted(reactions, key=lambda reaction: (-reaction.count, reaction.key)),
            unreadable=unreadable,
            truncated=cursor is not None,
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
        target = await self._decrypted_target(room_id, target)
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

    async def send(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        *,
        transaction_id: str | None = None,
    ) -> str:
        """Send one room event, encrypting it when the room is end-to-end encrypted."""
        quote_matrix_id(room_id, sigil="!", label="room ID")
        return await self._send(room_id, event_type, content, _transaction_path(transaction_id))

    async def attachment(self, room_id: str, event_id: str) -> EventAttachment:
        """Read a message's attachment, including decryption info for encrypted media."""
        target = await self._fetch_event(room_id, event_id)
        if _is_redacted(target):
            msg = "Matrix event was redacted"
            raise ValueError(msg)
        target = await self._decrypted_target(room_id, target)
        content = _mapping_field(target, "content", required=True)
        info = _mapping_field(content, "info")
        mimetype = _optional_string(info.get("mimetype"))
        if content.get("msgtype") in _MEDIA_MSGTYPES:
            url = _optional_string(content.get("url"))
            if url is not None:
                return EventAttachment(url=url, mimetype=mimetype)
            encrypted_file = content.get("file")
            if isinstance(encrypted_file, dict) and _optional_string(encrypted_file.get("url")):
                return EventAttachment(
                    url=encrypted_file["url"], mimetype=mimetype, encryption=encrypted_file
                )
        msg = "Matrix event has no attachment"
        raise ValueError(msg)

    async def decrypt_raw(
        self, room_id: str, raw_events: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
        """Decrypt encrypted events; map each encrypted event ID to its decryption error."""
        events: list[dict[str, Any]] = []
        encryption: dict[str, str | None] = {}
        for received in raw_events:
            # Decryption provenance lives on the event itself; never accept it from a server.
            raw = {key: value for key, value in received.items() if key != _DECRYPTED}
            if raw.get("type") != "m.room.encrypted":
                events.append(raw)
                continue
            if _is_redacted(raw):
                result = DecryptedEvent(raw)
            elif self.crypto is None:
                result = DecryptedEvent(raw, E2EE_UNSUPPORTED)
            else:
                result = await self.crypto.decrypt(room_id, raw)
            events.append(
                result.event if result.error is not None else {**result.event, _DECRYPTED: True}
            )
            event_id = _optional_string(raw.get("event_id"))
            if event_id is not None:
                encryption[event_id] = result.error
        return events, encryption

    async def _decrypted_target(self, room_id: str, target: dict[str, Any]) -> dict[str, Any]:
        [decrypted], encryption = await self.decrypt_raw(room_id, [target])
        error = encryption.get(cast("str", target["event_id"]))
        if error is not None:
            msg = f"Encrypted Matrix event could not be decrypted: {error}"
            raise ValueError(msg)
        return decrypted

    async def _send(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        transaction: str,
    ) -> str:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        if await self.http.room_is_encrypted(room_id):
            if self.crypto is None:
                msg = "Sending to end-to-end encrypted Matrix rooms is not supported in this mode"
                raise RuntimeError(msg)
            event_type, content = await self.crypto.encrypt(room_id, event_type, content)
        payload = await self.http.json(
            "PUT",
            f"/_matrix/client/v3/rooms/{room}/send/{event_type}/{transaction}",
            body=content,
        )
        return _required_string(payload, "event_id", context="Matrix send response")

    async def _expand_many(
        self,
        room_id: str,
        raw_events: list[dict[str, Any]],
        *,
        replacements: dict[str, list[dict[str, Any]]] | None = None,
        relation_budget: _RelationFetchBudget,
    ) -> tuple[list[TimelineEvent], bool]:
        events: list[TimelineEvent] = []
        truncated = False
        for raw in raw_events:
            event_id = _optional_string(raw.get("event_id")) or ""
            event, event_truncated = await self._expand_one(
                room_id,
                raw,
                replacements=[] if replacements is None else replacements.get(event_id, []),
                relation_budget=relation_budget,
            )
            events.append(event)
            truncated = truncated or event_truncated
        return events, truncated

    async def _expand_one(
        self,
        room_id: str,
        raw: dict[str, Any],
        *,
        replacements: list[dict[str, Any]] | None = None,
        relation_budget: _RelationFetchBudget,
    ) -> tuple[TimelineEvent, bool]:
        original = _timeline_event(raw)
        if (
            original.redacted
            or original.type == "m.room.encrypted"
            or _is_replacement(raw)
            or "state_key" in raw
        ):
            return original, False
        bundle, bundle_present = _bundled_replacement(raw)
        # The crypto layer already reduced an untrusted bundle to a bare reference.
        candidates = trusted_replacements(replacements or [], original_encrypted=is_decrypted(raw))
        if bundle is not None:
            candidates.append(bundle)
        valid = [
            replacement
            for replacement in candidates
            if _valid_replacement(room_id, raw, replacement)
        ]
        if valid:
            return _apply_replacement(original, max(valid, key=_replacement_order)), False
        if not bundle_present:
            return original, False
        recovered, truncated = await self._replacement_relations(
            room_id,
            original.event_id,
            relation_budget=relation_budget,
        )
        valid = [
            replacement
            for replacement in trusted_replacements(recovered, original_encrypted=is_decrypted(raw))
            if _valid_replacement(room_id, raw, replacement)
        ]
        if not valid:
            return original, truncated
        latest = max(valid, key=_replacement_order)
        return _apply_replacement(original, latest), truncated

    async def _replacement_relations(
        self,
        room_id: str,
        event_id: str,
        *,
        relation_budget: _RelationFetchBudget,
    ) -> tuple[list[dict[str, Any]], bool]:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        event = quote_matrix_id(event_id, sigil="$", label="event ID")
        # No event type filter: replacements in encrypted rooms are m.room.encrypted.
        path = f"/_matrix/client/v1/rooms/{room}/relations/{event}/m.replace"
        replacements: list[dict[str, Any]] = []
        cursor: str | None = None
        for page_number in range(_MAX_RELATION_PAGES):
            if not relation_budget.take():
                return replacements, True
            params: dict[str, str | int] = {"dir": "b", "limit": _RELATION_PAGE_LIMIT}
            if cursor is not None:
                params["from"] = cursor
            payload = await self.http.json("GET", path, params=params)
            chunk = _event_list(payload, "chunk", required=True)
            _require_page_bound(chunk, _RELATION_PAGE_LIMIT)
            decrypted, _ = await self.decrypt_raw(room_id, chunk)
            replacements.extend(decrypted)
            cursor = _optional_string_field(payload, "next_batch")
            if cursor is None:
                return replacements, False
            if page_number + 1 == _MAX_RELATION_PAGES:
                return replacements, True
        return replacements, cursor is not None


class _RelationFetchBudget:
    def __init__(self) -> None:
        self.remaining = _MAX_RELATION_REQUESTS

    def take(self) -> bool:
        if self.remaining == 0:
            return False
        self.remaining -= 1
        return True


def is_decrypted(raw: dict[str, Any]) -> bool:
    """Whether decrypt_raw produced this event by decrypting it."""
    return raw.get(_DECRYPTED) is True


def trusted_replacements(
    candidates: list[dict[str, Any]], *, original_encrypted: bool
) -> list[dict[str, Any]]:
    """Only successfully decrypted replacements may edit an encrypted message."""
    if not original_encrypted:
        return candidates
    return [candidate for candidate in candidates if is_decrypted(candidate)]


def mark_encryption(event: TimelineEvent, encryption: dict[str, str | None]) -> TimelineEvent:
    """Record whether an event was encrypted and why it could not be decrypted."""
    if event.event_id not in encryption:
        return event
    return event.model_copy(
        update={"encrypted": True, "decryption_error": encryption[event.event_id]}
    )


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
        relation_type=_optional_string(relation.get("rel_type")),
        media=None if redacted else _media_metadata(content, msgtype),
        redacted=redacted,
    )


def _media_metadata(content: dict[str, Any], msgtype: str | None) -> MediaMetadata | None:
    if msgtype not in _MEDIA_MSGTYPES:
        return None
    url = _optional_string(content.get("url"))
    encrypted = False
    encrypted_file = content.get("file")
    if url is None and isinstance(encrypted_file, dict):
        url = _optional_string(encrypted_file.get("url"))
        encrypted = True
    if url is None:
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
        encrypted=encrypted,
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


def _replacement_map(raw_events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    replacements: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_events:
        target = _relationship_id(raw, "m.replace")
        if target is not None:
            replacements.setdefault(target, []).append(raw)
    return replacements


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


def _thread_bundle(raw: dict[str, Any]) -> dict[str, Any]:
    unsigned = raw.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, dict) else None
    bundle = relations.get("m.thread") if isinstance(relations, dict) else None
    return bundle if isinstance(bundle, dict) else {}


def _reaction_relation(raw: dict[str, Any], readable: dict[str, Any]) -> dict[str, Any] | None:
    # Encryption moves relations into the cleartext wrapper, so keys stay readable.
    if readable.get("type") == "m.reaction":
        return _annotation(readable) or _annotation(raw)
    if readable.get("type") == "m.room.encrypted":
        return _annotation(raw)
    return None


def _annotation(raw: dict[str, Any]) -> dict[str, Any] | None:
    content = raw.get("content")
    relation = content.get("m.relates_to") if isinstance(content, dict) else None
    if (
        isinstance(relation, dict)
        and relation.get("rel_type") == "m.annotation"
        and _optional_string(relation.get("key")) is not None
    ):
        return relation
    return None


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


def _page_limit(limit: int, *, maximum: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        msg = f"limit must be between 1 and {maximum}"
        raise ValueError(msg)
    return limit


def _require_cursor(cursor: str) -> str:
    if not isinstance(cursor, str) or not cursor:
        msg = "Matrix pagination cursor must not be empty"
        raise ValueError(msg)
    return cursor


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
