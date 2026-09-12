from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from http import HTTPStatus
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from aiohttp import ContentTypeError
from anyio import Path as AsyncPath
from nio import (
    AsyncClient,
    AsyncClientConfig,
    ErrorResponse,
    JoinedMembersResponse,
    JoinedRoomsResponse,
    MessageDirection,
    ProfileGetResponse,
    ProfileSetAvatarResponse,
    ProfileSetDisplayNameResponse,
    RoomGetEventResponse,
    RoomGetStateEventError,
    RoomGetStateEventResponse,
    RoomInviteResponse,
    RoomMessagesResponse,
    RoomPutStateResponse,
    RoomSendResponse,
    UploadResponse,
)
from nio.api import RelationshipType
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.http_headers import resolve_http_headers
from matrix_mcp.id_state import MatrixIdStore
from matrix_mcp.matrix_events import (
    MatrixEvents,
    MediaMetadata,
    TimelineEvent,
    normalize_timeline_event,
)
from matrix_mcp.matrix_http import MatrixHTTP
from matrix_mcp.matrix_media import MatrixMedia
from matrix_mcp.matrix_rooms import MatrixRooms
from matrix_mcp.tls import default_ssl_context


class MatrixRoom(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    room_id: str
    name: str | None = None


class MatrixRoomInfo(MatrixRoom):
    topic: str | None = None
    avatar_url: str | None = None


class MatrixProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    displayname: str | None = Field(
        default=None, validation_alias=AliasChoices("displayname", "display_name")
    )
    avatar_url: str | None = None


class MatrixRoomMembers(BaseModel):
    model_config = ConfigDict(frozen=True)

    members: list[MatrixProfile]
    total: int
    next_offset: int | None


class MatrixUserSearch(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: list[MatrixProfile]
    limited: bool


class MatrixEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    event_id: str
    sender: str
    timestamp_ms: int | None = None
    type: str = "m.room.message"
    msgtype: str | None = None
    body: str | None
    thread_id: str | None = None
    thread_ref: int | None = None
    reply_to: str | None = None
    media: MediaMetadata | None = None
    edited: bool = False
    redacted: bool = False


class MatrixDriver(Protocol):
    async def whoami(self) -> dict[str, str | None]: ...

    async def list_rooms(self) -> list[MatrixRoom]: ...

    async def list_room_members(
        self, room_id: str, *, limit: int = 100, offset: int = 0
    ) -> MatrixRoomMembers: ...

    async def invite_user(self, room_id: str, user_id: str) -> None: ...

    async def get_room_info(self, room_id: str) -> MatrixRoomInfo: ...

    async def set_room_name(self, room_id: str, name: str) -> str: ...

    async def set_room_topic(self, room_id: str, topic: str) -> str: ...

    async def set_room_avatar(self, room_id: str, avatar_url: str) -> str: ...

    async def get_profile(self, user_id: str | None = None) -> MatrixProfile: ...

    async def set_display_name(self, displayname: str) -> None: ...

    async def set_avatar(self, avatar_url: str) -> None: ...

    async def search_users(self, search_term: str, *, limit: int = 25) -> MatrixUserSearch: ...

    async def read_room_recent(self, room_id: str, *, limit: int = 20) -> list[MatrixEvent]: ...

    async def read_thread(
        self, room_id: str, thread_id: str, *, limit: int = 50
    ) -> list[MatrixEvent]: ...

    async def send_message(
        self,
        room_id: str,
        body: str,
        *,
        thread_id: str | None = None,
        mentions: list[str] | None = None,
    ) -> str: ...

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        *,
        thread_id: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str: ...


class NioMatrixDriver:
    def __init__(self, config: MatrixMCPConfig) -> None:
        token = config.access_token_value()
        if not token or not config.user_id or not config.device_id:
            msg = "Matrix credentials are incomplete. Run `matrix-mcp auth` first."
            raise RuntimeError(msg)
        self._config = config
        self.http = MatrixHTTP(config)
        self.events = MatrixEvents(self.http)
        self.rooms = MatrixRooms(self.http)
        self.media = MatrixMedia(self.http)
        self._client = AsyncClient(
            config.normalized_homeserver,
            config.user_id,
            config=AsyncClientConfig(
                custom_headers=resolve_http_headers(
                    config.http_headers,
                    config.http_header_commands,
                )
                or None
            ),
            # nio annotates ssl as bool but forwards it to aiohttp, which
            # accepts an SSLContext.
            ssl=default_ssl_context(),  # ty: ignore[invalid-argument-type]
        )
        self._client.restore_login(
            user_id=config.user_id,
            device_id=config.device_id,
            access_token=token,
        )

    async def close(self) -> None:
        await self._client.close()

    async def whoami(self) -> dict[str, str | None]:
        return {"user_id": self._config.user_id, "device_id": self._config.device_id}

    async def list_rooms(self) -> list[MatrixRoom]:
        response = await self._client.joined_rooms()
        if isinstance(response, JoinedRoomsResponse):
            names = await asyncio.gather(*(self._room_name(room_id) for room_id in response.rooms))
            return [
                MatrixRoom(room_id=room_id, name=name)
                for room_id, name in zip(response.rooms, names, strict=True)
            ]
        msg = f"Matrix joined_rooms failed: {response}"
        raise RuntimeError(msg)

    async def _room_name(self, room_id: str) -> str | None:
        response = await self._client.room_get_state_event(room_id, "m.room.name")
        if not isinstance(response, RoomGetStateEventResponse):
            return None
        name = response.content.get("name")
        return name if isinstance(name, str) else None

    async def list_room_members(
        self, room_id: str, *, limit: int = 100, offset: int = 0
    ) -> MatrixRoomMembers:
        _validate_limit(limit)
        if offset < 0:
            msg = "offset must be nonnegative"
            raise ValueError(msg)
        response = await self._client.joined_members(room_id)
        if isinstance(response, JoinedMembersResponse):
            members = sorted(response.members, key=lambda member: member.user_id)
            total = len(members)
            return MatrixRoomMembers(
                members=[
                    MatrixProfile(
                        user_id=member.user_id,
                        displayname=member.display_name,
                        avatar_url=member.avatar_url,
                    )
                    for member in members[offset : offset + limit]
                ],
                total=total,
                next_offset=offset + limit if offset + limit < total else None,
            )
        msg = f"Matrix joined_members failed: {response}"
        raise RuntimeError(msg)

    async def invite_user(self, room_id: str, user_id: str) -> None:
        _validate_user_id(user_id)
        response = await self._client.room_invite(room_id, user_id)
        if isinstance(response, RoomInviteResponse):
            return
        msg = f"Matrix room_invite failed: {response}"
        raise RuntimeError(msg)

    async def get_room_info(self, room_id: str) -> MatrixRoomInfo:
        name, topic, avatar_url = await asyncio.gather(
            self._room_state_value(room_id, "m.room.name", "name"),
            self._room_state_value(room_id, "m.room.topic", "topic"),
            self._room_state_value(room_id, "m.room.avatar", "url"),
        )
        return MatrixRoomInfo(room_id=room_id, name=name, topic=topic, avatar_url=avatar_url)

    async def _room_state_value(self, room_id: str, event_type: str, key: str) -> str | None:
        response = await self._client.room_get_state_event(room_id, event_type, state_key="")
        # Nio only classifies HTTP 404 as a room-state error itself.
        if (
            isinstance(response, RoomGetStateEventResponse)
            and response.transport_response is not None
            and response.transport_response.status >= HTTPStatus.BAD_REQUEST
        ):
            response = RoomGetStateEventError.from_dict(response.content, room_id)
        if isinstance(response, RoomGetStateEventResponse):
            value = response.content.get(key)
            return value if isinstance(value, str) else None
        if isinstance(response, RoomGetStateEventError) and response.status_code == "M_NOT_FOUND":
            return None
        msg = f"Matrix room_get_state_event failed: {response}"
        raise RuntimeError(msg)

    async def set_room_name(self, room_id: str, name: str) -> str:
        return await self._put_room_state(room_id, "m.room.name", {"name": name})

    async def set_room_topic(self, room_id: str, topic: str) -> str:
        return await self._put_room_state(room_id, "m.room.topic", {"topic": topic})

    async def set_room_avatar(self, room_id: str, avatar_url: str) -> str:
        _validate_avatar_url(avatar_url)
        return await self._put_room_state(room_id, "m.room.avatar", {"url": avatar_url})

    async def _put_room_state(self, room_id: str, event_type: str, content: dict[str, str]) -> str:
        response = await self._client.room_put_state(room_id, event_type, content, state_key="")
        if isinstance(response, RoomPutStateResponse):
            return cast("str", response.event_id)
        msg = f"Matrix room_put_state failed: {response}"
        raise RuntimeError(msg)

    async def get_profile(self, user_id: str | None = None) -> MatrixProfile:
        target = self._client.user_id if user_id is None else user_id
        _validate_user_id(target)
        response = await self._client.get_profile(target)
        if isinstance(response, ProfileGetResponse):
            return MatrixProfile(
                user_id=target,
                displayname=response.displayname,
                avatar_url=response.avatar_url,
            )
        msg = f"Matrix get_profile failed: {response}"
        raise RuntimeError(msg)

    async def set_display_name(self, displayname: str) -> None:
        response = await self._client.set_displayname(displayname)
        if isinstance(response, ProfileSetDisplayNameResponse):
            return
        msg = f"Matrix set_displayname failed: {response}"
        raise RuntimeError(msg)

    async def set_avatar(self, avatar_url: str) -> None:
        _validate_avatar_url(avatar_url)
        response = await self._client.set_avatar(avatar_url)
        if isinstance(response, ProfileSetAvatarResponse):
            return
        msg = f"Matrix set_avatar failed: {response}"
        raise RuntimeError(msg)

    async def search_users(self, search_term: str, *, limit: int = 25) -> MatrixUserSearch:
        _validate_limit(limit)
        if not search_term.strip():
            msg = "search_term must not be empty"
            raise ValueError(msg)
        # Nio has no user-directory method; reuse its public HTTP transport.
        headers = dict(self._client.config.custom_headers or {})
        headers.update(
            {
                "Authorization": f"Bearer {self._client.access_token}",
                "Content-Type": "application/json",
            }
        )
        async with await self._client.send(
            "POST",
            "/_matrix/client/v3/user_directory/search",
            data=json.dumps({"search_term": search_term, "limit": limit}),
            headers=headers,
        ) as response:
            try:
                payload = await response.json()
            except (ContentTypeError, ValueError) as exc:
                msg = "Matrix user search returned an invalid JSON response"
                raise RuntimeError(msg) from exc
            if response.status != HTTPStatus.OK:
                detail = (
                    ErrorResponse.from_dict(payload)
                    if isinstance(payload, dict)
                    else response.status
                )
                msg = f"Matrix user search failed: {detail}"
                raise RuntimeError(msg)
        try:
            result = MatrixUserSearch.model_validate(payload, strict=True)
        except ValidationError as exc:
            msg = "Matrix user search returned an invalid response"
            raise RuntimeError(msg) from exc
        return MatrixUserSearch(
            results=result.results[:limit],
            limited=result.limited or len(result.results) > limit,
        )

    async def read_room_recent(self, room_id: str, *, limit: int = 20) -> list[MatrixEvent]:
        response = await self._client.room_messages(
            room_id,
            direction=MessageDirection.back,
            limit=max(1, min(limit, 100)),
        )
        if isinstance(response, RoomMessagesResponse):
            return [
                event
                for raw in response.chunk
                if (event := _event_from_nio(room_id, raw)) is not None
            ]
        msg = f"Matrix room_messages failed: {response}"
        raise RuntimeError(msg)

    async def read_thread(
        self, room_id: str, thread_id: str, *, limit: int = 50
    ) -> list[MatrixEvent]:
        max_replies = max(1, min(limit, 100))
        raw_events: list[tuple[dict[str, Any], bool]] = []

        root_response = await self._client.room_get_event(room_id, thread_id)
        if isinstance(root_response, RoomGetEventResponse):
            root = _source_from_nio(root_response.event)
            if root is not None:
                raw_events.append((root, False))

        reply_count = 0
        async for raw in self._client.room_get_event_relations(
            room_id,
            thread_id,
            rel_type=RelationshipType.thread,
            event_type="m.room.message",
            direction=MessageDirection.front,
            limit=max_replies,
        ):
            source = _source_from_nio(raw)
            if source is not None:
                raw_events.append((source, True))
            reply_count += 1
            if reply_count >= max_replies:
                break

        events: list[MatrixEvent] = []
        for raw, is_reply in raw_events:
            event = await self._event_with_latest_edit(room_id, raw, page_size=max_replies)
            if is_reply and event.thread_id is None:
                event = event.model_copy(update={"thread_id": thread_id})
            events.append(event)
        return sorted(events, key=_event_sort_key)

    async def _event_with_latest_edit(
        self, room_id: str, raw: dict[str, Any], *, page_size: int
    ) -> MatrixEvent:
        event = normalize_timeline_event(room_id, raw)
        if event.edited or event.redacted:
            return _event_from_timeline(event)

        latest: tuple[tuple[int, str], TimelineEvent] | None = None
        scanned = 0
        async for replacement in self._client.room_get_event_relations(
            room_id,
            event.event_id,
            rel_type=RelationshipType.replacement,
            event_type="m.room.message",
            direction=MessageDirection.back,
            limit=page_size,
        ):
            replacement_source = _source_from_nio(replacement)
            if replacement_source is not None:
                updated = normalize_timeline_event(
                    room_id,
                    raw,
                    replacement=replacement_source,
                )
                if updated.edited:
                    key = (
                        cast("int", replacement_source["origin_server_ts"]),
                        cast("str", replacement_source["event_id"]),
                    )
                    if latest is None or key > latest[0]:
                        latest = key, updated
            scanned += 1
            if scanned >= page_size:
                break

        return _event_from_timeline(event if latest is None else latest[1])

    async def send_message(
        self,
        room_id: str,
        body: str,
        *,
        thread_id: str | None = None,
        mentions: list[str] | None = None,
    ) -> str:
        if mentions is not None:
            for user_id in mentions:
                if re.fullmatch(r"@[^\s:]+:[^\s]+", user_id) is None:
                    msg = f"Invalid Matrix user ID: {user_id}"
                    raise ValueError(msg)
        content: dict[str, object] = {
            "body": body,
            "msgtype": "m.text",
        }
        if mentions is not None:
            content["m.mentions"] = {"user_ids": mentions}
        if thread_id:
            content["m.relates_to"] = {
                "event_id": thread_id,
                "is_falling_back": False,
                "rel_type": "m.thread",
            }
        response = await self._client.room_send(room_id, "m.room.message", content)
        if isinstance(response, RoomSendResponse):
            return cast("str", response.event_id)
        msg = f"Matrix room_send failed: {response}"
        raise RuntimeError(msg)

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        *,
        thread_id: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        path = await AsyncPath(file_path).expanduser()
        display_name = filename or path.name
        resolved_content_type = content_type or mimetypes.guess_type(display_name)[0]
        resolved_content_type = resolved_content_type or "application/octet-stream"
        size = (await path.stat()).st_size

        def upload_path(_got_429: int, _got_timeouts: int) -> str:
            return str(path)

        upload_response, _decryption_info = await self._client.upload(
            upload_path,
            content_type=resolved_content_type,
            filename=display_name,
            filesize=size,
        )
        if isinstance(upload_response, UploadResponse):
            content = _file_message_content(
                content_uri=upload_response.content_uri,
                filename=display_name,
                content_type=resolved_content_type,
                size=size,
                thread_id=thread_id,
            )
            response = await self._client.room_send(room_id, "m.room.message", content)
            if isinstance(response, RoomSendResponse):
                return cast("str", response.event_id)
            msg = f"Matrix room_send failed: {response}"
            raise RuntimeError(msg)
        msg = f"Matrix media upload failed: {upload_response}"
        raise RuntimeError(msg)

    async def aclose(self) -> None:
        await self._client.close()


class MatrixAPIClient:
    def __init__(
        self,
        config: MatrixMCPConfig | None = None,
        *,
        driver: MatrixDriver | None = None,
        id_store: MatrixIdStore | None = None,
    ) -> None:
        self._owned_driver: NioMatrixDriver | None = None
        if driver is not None:
            self._driver = driver
            self._id_store = id_store
            return
        config = config or MatrixMCPConfig.load()
        self._owned_driver = NioMatrixDriver(config)
        self._driver = self._owned_driver
        self._id_store = id_store or MatrixIdStore.for_config(config)

    async def aclose(self) -> None:
        """Close the internally created driver; injected drivers belong to the caller."""
        if self._owned_driver is not None:
            await self._owned_driver.aclose()

    @property
    def events(self) -> MatrixEvents:
        return cast("MatrixEvents", self._grouped_driver_property("events"))

    @property
    def rooms(self) -> MatrixRooms:
        return cast("MatrixRooms", self._grouped_driver_property("rooms"))

    @property
    def media(self) -> MatrixMedia:
        return cast("MatrixMedia", self._grouped_driver_property("media"))

    async def whoami(self) -> dict[str, str | None]:
        return await self._driver.whoami()

    async def list_rooms(self) -> list[MatrixRoom]:
        rooms = await self._driver.list_rooms()
        return [self._with_room_ref(room) for room in rooms]

    async def list_room_members(
        self, room_id: str | int, *, limit: int = 100, offset: int = 0
    ) -> MatrixRoomMembers:
        return await self._driver.list_room_members(
            self._resolve_room(room_id), limit=limit, offset=offset
        )

    async def invite_user(self, room_id: str | int, user_id: str) -> None:
        await self._driver.invite_user(self._resolve_room(room_id), user_id)

    async def get_room_info(self, room_id: str | int) -> MatrixRoomInfo:
        room = await self._driver.get_room_info(self._resolve_room(room_id))
        return self._with_room_ref(room)

    async def set_room_name(self, room_id: str | int, name: str) -> str:
        return await self._driver.set_room_name(self._resolve_room(room_id), name)

    async def set_room_topic(self, room_id: str | int, topic: str) -> str:
        return await self._driver.set_room_topic(self._resolve_room(room_id), topic)

    async def set_room_avatar(self, room_id: str | int, avatar_url: str) -> str:
        return await self._driver.set_room_avatar(self._resolve_room(room_id), avatar_url)

    async def get_profile(self, user_id: str | None = None) -> MatrixProfile:
        return await self._driver.get_profile(user_id)

    async def set_display_name(self, displayname: str) -> None:
        await self._driver.set_display_name(displayname)

    async def set_avatar(self, avatar_url: str) -> None:
        await self._driver.set_avatar(avatar_url)

    async def search_users(self, search_term: str, *, limit: int = 25) -> MatrixUserSearch:
        return await self._driver.search_users(search_term, limit=limit)

    async def read_room_recent(self, room_id: str | int, *, limit: int = 20) -> list[MatrixEvent]:
        resolved_room_id = self._resolve_room(room_id)
        grouped_events = getattr(self._driver, "events", None)
        if isinstance(grouped_events, MatrixEvents):
            page = await grouped_events.history(resolved_room_id, limit=limit)
            events = [_event_from_timeline(event) for event in page.events]
        else:
            events = await self._driver.read_room_recent(resolved_room_id, limit=limit)
        return [self._with_event_refs(event) for event in events]

    async def read_thread(
        self, room_id: str | int, thread_id: str | int, *, limit: int = 50
    ) -> list[MatrixEvent]:
        resolved_room_id = self._resolve_room(room_id)
        resolved_thread_id = self._resolve_event(thread_id)
        events = await self._driver.read_thread(resolved_room_id, resolved_thread_id, limit=limit)
        return [self._with_event_refs(event, thread_root_id=resolved_thread_id) for event in events]

    async def send_message(
        self,
        room_id: str | int,
        body: str,
        *,
        thread_id: str | int | None = None,
        mentions: list[str] | None = None,
    ) -> str:
        return await self._driver.send_message(
            self._resolve_room(room_id),
            body,
            thread_id=self._resolve_optional_event(thread_id),
            mentions=mentions,
        )

    async def send_file(
        self,
        room_id: str | int,
        file_path: str,
        *,
        thread_id: str | int | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        return await self._driver.send_file(
            self._resolve_room(room_id),
            file_path,
            thread_id=self._resolve_optional_event(thread_id),
            filename=filename,
            content_type=content_type,
        )

    def _with_room_ref[RoomT: MatrixRoom](self, room: RoomT) -> RoomT:
        if self._id_store is None:
            return room
        return room.model_copy(update={"id": self._id_store.room_ref(room.room_id)})

    def _with_event_refs(
        self,
        event: MatrixEvent,
        *,
        thread_root_id: str | None = None,
    ) -> MatrixEvent:
        if self._id_store is None:
            return event
        event_ref = self._id_store.event_ref(event.event_id)
        raw_thread_id = event.thread_id or thread_root_id
        thread_ref = self._id_store.event_ref(raw_thread_id) if raw_thread_id else None
        return event.model_copy(update={"id": event_ref, "thread_ref": thread_ref})

    def _resolve_room(self, room_id_or_ref: str | int) -> str:
        if self._id_store is None:
            return str(room_id_or_ref)
        return self._id_store.resolve_room(room_id_or_ref)

    def _resolve_event(self, event_id_or_ref: str | int) -> str:
        if self._id_store is None:
            return str(event_id_or_ref)
        return self._id_store.resolve_event(event_id_or_ref)

    def _resolve_optional_event(self, event_id_or_ref: str | int | None) -> str | None:
        if event_id_or_ref is None:
            return None
        return self._resolve_event(event_id_or_ref)

    def _grouped_driver_property(self, name: str) -> object:
        value = getattr(self._driver, name, None)
        if value is not None:
            return value
        msg = f"The configured Matrix driver does not support {name} operations"
        raise RuntimeError(msg)


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:  # noqa: PLR2004 - Public tool page-size bound.
        msg = "limit must be between 1 and 100"
        raise ValueError(msg)


def _validate_user_id(user_id: str) -> None:
    if re.fullmatch(r"@[^\s:]+:[^\s/?#@]+", user_id) is None:
        msg = f"Invalid Matrix user ID: {user_id}"
        raise ValueError(msg)


def _validate_avatar_url(avatar_url: str) -> None:
    if not avatar_url:
        return
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    hostname = rf"(?:{label}\.)*{label}\.?"
    match = re.fullmatch(
        rf"mxc://(?:{hostname}|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?/"
        r"[A-Za-z0-9_-]+",
        avatar_url,
    )
    try:
        valid = match is not None and urlsplit(avatar_url).port != 0
    except ValueError:
        valid = False
    if not valid:
        msg = "avatar_url must be a valid mxc://server/media_id URI or an empty string"
        raise ValueError(msg)


def _event_from_nio(room_id: str, raw: object) -> MatrixEvent | None:
    source = _source_from_nio(raw)
    if source is None:
        return None
    return _event_from_timeline(normalize_timeline_event(room_id, source))


def _source_from_nio(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    source = getattr(raw, "source", None)
    return cast("dict[str, Any]", source) if isinstance(source, dict) else None


def _event_from_timeline(event: TimelineEvent) -> MatrixEvent:
    return MatrixEvent(
        event_id=event.event_id,
        sender=event.sender,
        timestamp_ms=event.timestamp_ms,
        type=event.type,
        msgtype=event.msgtype,
        body=event.body,
        thread_id=event.thread_id,
        reply_to=event.reply_to,
        media=event.media,
        edited=event.edited,
        redacted=event.redacted,
    )


def _event_sort_key(event: MatrixEvent) -> tuple[int, str]:
    return (event.timestamp_ms if event.timestamp_ms is not None else -1, event.event_id)


def _file_message_content(
    *,
    content_uri: str,
    filename: str,
    content_type: str,
    size: int,
    thread_id: str | None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "body": filename,
        "filename": filename,
        "info": {
            "mimetype": content_type,
            "size": size,
        },
        "msgtype": "m.file",
        "url": content_uri,
    }
    if thread_id:
        content["m.relates_to"] = {
            "event_id": thread_id,
            "is_falling_back": False,
            "rel_type": "m.thread",
        }
    return content
