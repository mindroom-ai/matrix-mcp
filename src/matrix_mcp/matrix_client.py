from __future__ import annotations

from typing import Protocol

from nio import (
    AsyncClient,
    JoinedRoomsResponse,
    MessageDirection,
    RoomGetStateEventResponse,
    RoomMessagesResponse,
    RoomMessageText,
    RoomSendResponse,
)
from pydantic import BaseModel, ConfigDict

from matrix_mcp.config import MatrixMCPConfig


class MatrixRoom(BaseModel):
    model_config = ConfigDict(frozen=True)

    room_id: str
    name: str | None = None


class MatrixEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    sender: str
    timestamp_ms: int | None = None
    body: str
    thread_id: str | None = None


class MatrixDriver(Protocol):
    async def whoami(self) -> dict[str, str | None]: ...

    async def list_rooms(self) -> list[MatrixRoom]: ...

    async def read_room_recent(self, room_id: str, *, limit: int = 20) -> list[MatrixEvent]: ...

    async def send_message(self, room_id: str, body: str, *, thread_id: str | None = None) -> str: ...


class NioMatrixDriver:
    def __init__(self, config: MatrixMCPConfig) -> None:
        token = config.access_token_value()
        if not token or not config.user_id or not config.device_id:
            raise RuntimeError("Matrix credentials are incomplete. Run `matrix-mcp auth` first.")
        self._config = config
        self._client = AsyncClient(config.normalized_homeserver, config.user_id)
        self._client.restore_login(
            user_id=config.user_id,
            device_id=config.device_id,
            access_token=token,
        )

    async def whoami(self) -> dict[str, str | None]:
        return {"user_id": self._config.user_id, "device_id": self._config.device_id}

    async def list_rooms(self) -> list[MatrixRoom]:
        response = await self._client.joined_rooms()
        if not isinstance(response, JoinedRoomsResponse):
            raise RuntimeError(f"Matrix joined_rooms failed: {response}")
        rooms: list[MatrixRoom] = []
        for room_id in response.rooms:
            rooms.append(MatrixRoom(room_id=room_id, name=await self._room_name(room_id)))
        return rooms

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
        if not isinstance(response, RoomMessagesResponse):
            raise RuntimeError(f"Matrix room_messages failed: {response}")
        return [event for raw in response.chunk if (event := _event_from_nio(raw)) is not None]

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
        if not isinstance(response, RoomSendResponse):
            raise RuntimeError(f"Matrix room_send failed: {response}")
        return response.event_id

    async def aclose(self) -> None:
        await self._client.close()


class MatrixAPIClient:
    def __init__(
        self,
        config: MatrixMCPConfig | None = None,
        *,
        driver: MatrixDriver | None = None,
    ) -> None:
        if driver is None and config is None:
            config = MatrixMCPConfig.load()
        self._driver = driver or NioMatrixDriver(config)  # type: ignore[arg-type]

    async def whoami(self) -> dict[str, str | None]:
        return await self._driver.whoami()

    async def list_rooms(self) -> list[MatrixRoom]:
        return await self._driver.list_rooms()

    async def read_room_recent(self, room_id: str, *, limit: int = 20) -> list[MatrixEvent]:
        return await self._driver.read_room_recent(room_id, limit=limit)

    async def send_message(self, room_id: str, body: str, *, thread_id: str | None = None) -> str:
        return await self._driver.send_message(room_id, body, thread_id=thread_id)


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
