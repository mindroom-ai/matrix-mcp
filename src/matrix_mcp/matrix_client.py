from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Protocol, cast

from nio import (
    AsyncClient,
    AsyncClientConfig,
    JoinedRoomsResponse,
    MessageDirection,
    RoomGetEventResponse,
    RoomGetStateEventResponse,
    RoomMessagesResponse,
    RoomMessageText,
    RoomSendResponse,
    UploadResponse,
)
from nio.api import RelationshipType
from pydantic import BaseModel, ConfigDict

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.http_headers import resolve_http_headers
from matrix_mcp.id_state import MatrixIdStore


class MatrixRoom(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    room_id: str
    name: str | None = None


class MatrixEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    event_id: str
    sender: str
    timestamp_ms: int | None = None
    body: str
    thread_id: str | None = None
    thread_ref: int | None = None


class MatrixDriver(Protocol):
    async def whoami(self) -> dict[str, str | None]: ...

    async def list_rooms(self) -> list[MatrixRoom]: ...

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
        )
        self._client.restore_login(
            user_id=config.user_id,
            device_id=config.device_id,
            access_token=token,
        )

    async def whoami(self) -> dict[str, str | None]:
        return {"user_id": self._config.user_id, "device_id": self._config.device_id}

    async def list_rooms(self) -> list[MatrixRoom]:
        response = await self._client.joined_rooms()
        if isinstance(response, JoinedRoomsResponse):
            return [
                MatrixRoom(room_id=room_id, name=await self._room_name(room_id))
                for room_id in response.rooms
            ]
        msg = f"Matrix joined_rooms failed: {response}"
        raise RuntimeError(msg)

    async def _room_name(self, room_id: str) -> str | None:
        response = await self._client.room_get_state_event(room_id, "m.room.name")
        if not isinstance(response, RoomGetStateEventResponse):
            return None
        name = response.content.get("name")
        return name if isinstance(name, str) else None

    async def read_room_recent(self, room_id: str, *, limit: int = 20) -> list[MatrixEvent]:
        response = await self._client.room_messages(
            room_id,
            direction=MessageDirection.back,
            limit=max(1, min(limit, 100)),
        )
        if isinstance(response, RoomMessagesResponse):
            return [event for raw in response.chunk if (event := _event_from_nio(raw)) is not None]
        msg = f"Matrix room_messages failed: {response}"
        raise RuntimeError(msg)

    async def read_thread(
        self, room_id: str, thread_id: str, *, limit: int = 50
    ) -> list[MatrixEvent]:
        max_replies = max(1, min(limit, 100))
        events: list[MatrixEvent] = []

        root_response = await self._client.room_get_event(room_id, thread_id)
        if isinstance(root_response, RoomGetEventResponse):
            root = _event_from_nio(root_response.event)
            if root is not None:
                events.append(root)

        async for raw in self._client.room_get_event_relations(
            room_id,
            thread_id,
            rel_type=RelationshipType.thread,
            event_type="m.room.message",
            direction=MessageDirection.front,
            limit=max_replies,
        ):
            event = _event_from_nio(raw)
            if event is not None:
                events.append(event)

        return sorted(events, key=_event_sort_key)

    async def send_message(self, room_id: str, body: str, *, thread_id: str | None = None) -> str:
        content: dict[str, object] = {
            "body": body,
            "msgtype": "m.text",
        }
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
        path = Path(file_path).expanduser()
        display_name = filename or path.name
        resolved_content_type = content_type or mimetypes.guess_type(display_name)[0]
        resolved_content_type = resolved_content_type or "application/octet-stream"
        size = path.stat().st_size

        with path.open("rb") as file:
            upload_response, _decryption_info = await self._client.upload(
                file,
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
        if driver is not None:
            self._driver = driver
            self._id_store = id_store
            return
        config = config or MatrixMCPConfig.load()
        self._driver = NioMatrixDriver(config)
        self._id_store = id_store or MatrixIdStore.for_config(config)

    async def whoami(self) -> dict[str, str | None]:
        return await self._driver.whoami()

    async def list_rooms(self) -> list[MatrixRoom]:
        rooms = await self._driver.list_rooms()
        return [self._with_room_ref(room) for room in rooms]

    async def read_room_recent(self, room_id: str | int, *, limit: int = 20) -> list[MatrixEvent]:
        resolved_room_id = self._resolve_room(room_id)
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
    ) -> str:
        return await self._driver.send_message(
            self._resolve_room(room_id),
            body,
            thread_id=self._resolve_optional_event(thread_id),
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

    def _with_room_ref(self, room: MatrixRoom) -> MatrixRoom:
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


def _event_from_nio(raw: object) -> MatrixEvent | None:
    if not isinstance(raw, RoomMessageText):
        return None
    thread_id = None
    relates_to = getattr(raw, "relates_to", None)
    if isinstance(relates_to, dict) and relates_to.get("rel_type") == "m.thread":
        raw_thread_id = relates_to.get("event_id")
        thread_id = raw_thread_id if isinstance(raw_thread_id, str) else None
    return MatrixEvent(
        event_id=raw.event_id,
        sender=raw.sender,
        timestamp_ms=raw.server_timestamp,
        body=raw.body,
        thread_id=thread_id,
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
