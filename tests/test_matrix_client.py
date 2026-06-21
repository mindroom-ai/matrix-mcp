from __future__ import annotations

from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar, cast

import pytest
from anyio import Path as AsyncPath
from nio import (
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

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.id_state import MatrixIdStore
from matrix_mcp.matrix_client import MatrixAPIClient, MatrixEvent, MatrixRoom, NioMatrixDriver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


class FakeDriver:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.files: list[tuple[str, str, str | None, str | None, str | None]] = []
        self.room_member_calls = 0

    async def whoami(self) -> dict[str, str | None]:
        return {"user_id": "@alice:example.com", "device_id": "CLAUDECODE"}

    async def list_rooms(self) -> list[MatrixRoom]:
        return [MatrixRoom(room_id="!room:example.com", name="Mind")]

    async def read_room_recent(self, room_id: str, *, limit: int = 20) -> list[MatrixEvent]:
        assert room_id == "!room:example.com"
        assert limit == 10
        return [
            MatrixEvent(
                event_id="$event1",
                sender="@alice:example.com",
                timestamp_ms=123,
                body="hello",
                thread_id=None,
            ),
        ]

    async def read_thread(
        self, room_id: str, thread_id: str, *, limit: int = 50
    ) -> list[MatrixEvent]:
        assert room_id == "!room:example.com"
        assert thread_id == "$root"
        assert limit == 25
        return [
            MatrixEvent(
                event_id="$root",
                sender="@alice:example.com",
                timestamp_ms=100,
                body="root",
                thread_id=None,
            ),
            MatrixEvent(
                event_id="$reply",
                sender="@bob:example.com",
                timestamp_ms=200,
                body="reply",
                thread_id="$root",
            ),
        ]

    async def send_message(self, room_id: str, body: str, *, thread_id: str | None = None) -> str:
        self.sent.append((room_id, body, thread_id))
        return "$sent"

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        *,
        thread_id: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        self.files.append((room_id, file_path, thread_id, filename, content_type))
        return "$file"


def text_event(
    event_id: str,
    *,
    sender: str = "@alice:example.com",
    timestamp_ms: int = 123,
    body: str = "hello",
    thread_id: str | None = None,
) -> RoomMessageText:
    event = RoomMessageText(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "content": {"body": body, "msgtype": "m.text"},
        },
        body,
        None,
        None,
    )
    if thread_id is not None:
        cast("Any", event).relates_to = {"rel_type": "m.thread", "event_id": thread_id}
    return event


def edit_event(
    event_id: str,
    *,
    replaces: str,
    sender: str = "@alice:example.com",
    timestamp_ms: int = 123,
    body: str = "edited",
) -> RoomMessageText:
    return RoomMessageText(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": replaces},
            },
        },
        f"* {body}",
        None,
        None,
    )


class FakeNioClient:
    instances: ClassVar[list[FakeNioClient]] = []

    def __init__(
        self,
        homeserver: str,
        user_id: str,
        *,
        config: AsyncClientConfig,
    ) -> None:
        self.homeserver = homeserver
        self.user_id = user_id
        self.config = config
        self.restore_login_call: dict[str, str] | None = None
        self.room_messages_call: dict[str, object] | None = None
        self.relations_call: dict[str, object] | None = None
        self.replacement_events: dict[str, list[RoomMessageText]] = {}
        self.replacement_calls: list[dict[str, object]] = []
        self.room_send_calls: list[tuple[str, str, dict[str, object]]] = []
        self.upload_call: dict[str, object] | None = None
        self.closed = False
        FakeNioClient.instances.append(self)

    def restore_login(self, *, user_id: str, device_id: str, access_token: str) -> None:
        self.restore_login_call = {
            "user_id": user_id,
            "device_id": device_id,
            "access_token": access_token,
        }

    async def joined_rooms(self) -> JoinedRoomsResponse:
        return JoinedRoomsResponse(["!room:example.com"])

    async def room_get_state_event(
        self, room_id: str, event_type: str
    ) -> RoomGetStateEventResponse:
        assert room_id == "!room:example.com"
        assert event_type == "m.room.name"
        return RoomGetStateEventResponse(
            {"name": "General"},
            "m.room.name",
            "",
            room_id,
        )

    async def room_messages(
        self,
        room_id: str,
        *,
        direction: MessageDirection,
        limit: int,
    ) -> RoomMessagesResponse:
        self.room_messages_call = {
            "room_id": room_id,
            "direction": direction,
            "limit": limit,
        }
        return RoomMessagesResponse(
            room_id,
            [text_event("$event", body="recent"), cast("Any", object())],
            "start",
            "end",
        )

    async def room_get_event(self, room_id: str, event_id: str) -> RoomGetEventResponse:
        assert room_id == "!room:example.com"
        assert event_id == "$root"
        response = RoomGetEventResponse()
        response.event = text_event("$root", timestamp_ms=100, body="root")
        return response

    async def room_get_event_relations(
        self,
        room_id: str,
        event_id: str,
        **kwargs: object,
    ) -> AsyncIterator[RoomMessageText]:
        call = {"room_id": room_id, "event_id": event_id, **kwargs}
        if kwargs.get("rel_type") == RelationshipType.replacement:
            self.replacement_calls.append(call)
            for event in self.replacement_events.get(event_id, []):
                yield event
            return

        self.relations_call = call
        yield text_event(
            "$reply",
            sender="@bob:example.com",
            timestamp_ms=200,
            body="reply",
            thread_id="$root",
        )

    async def room_send(
        self, room_id: str, event_type: str, content: dict[str, object]
    ) -> RoomSendResponse:
        self.room_send_calls.append((room_id, event_type, content))
        return RoomSendResponse(f"$sent{len(self.room_send_calls)}", room_id)

    async def upload(
        self,
        file: object,
        *,
        content_type: str,
        filename: str,
        filesize: int,
    ) -> tuple[UploadResponse, None]:
        if callable(file):
            provider = cast("Callable[[int, int], str]", file)
            content = await AsyncPath(provider(0, 0)).read_bytes()
        else:
            content = cast("BinaryIO", file).read()
        self.upload_call = {
            "content": content,
            "content_type": content_type,
            "filename": filename,
            "filesize": filesize,
        }
        return UploadResponse("mxc://example.com/report"), None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_nio_driver_uses_matrix_client_for_room_and_message_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeNioClient.instances.clear()
    monkeypatch.setattr("matrix_mcp.matrix_client.AsyncClient", FakeNioClient)
    path = tmp_path / "report.txt"
    path.write_text("hello", encoding="utf-8")

    driver = NioMatrixDriver(
        MatrixMCPConfig(
            homeserver="https://matrix.example.com/",
            user_id="@alice:example.com",
            device_id="TESTDEVICE",
            access_token="test-token",
            http_headers={"X-Access": "secret"},
        )
    )
    nio_client = FakeNioClient.instances[0]

    assert nio_client.homeserver == "https://matrix.example.com"
    assert nio_client.user_id == "@alice:example.com"
    assert nio_client.config.custom_headers == {"X-Access": "secret"}
    assert nio_client.restore_login_call == {
        "user_id": "@alice:example.com",
        "device_id": "TESTDEVICE",
        "access_token": "test-token",
    }
    assert await driver.whoami() == {
        "user_id": "@alice:example.com",
        "device_id": "TESTDEVICE",
    }
    assert await driver.list_rooms() == [MatrixRoom(room_id="!room:example.com", name="General")]

    recent = await driver.read_room_recent("!room:example.com", limit=500)
    assert recent == [
        MatrixEvent(
            event_id="$event",
            sender="@alice:example.com",
            timestamp_ms=123,
            body="recent",
            thread_id=None,
        )
    ]
    assert nio_client.room_messages_call == {
        "room_id": "!room:example.com",
        "direction": MessageDirection.back,
        "limit": 100,
    }

    thread = await driver.read_thread("!room:example.com", "$root", limit=500)
    assert thread == [
        MatrixEvent(
            event_id="$root",
            sender="@alice:example.com",
            timestamp_ms=100,
            body="root",
            thread_id=None,
        ),
        MatrixEvent(
            event_id="$reply",
            sender="@bob:example.com",
            timestamp_ms=200,
            body="reply",
            thread_id="$root",
        ),
    ]
    assert nio_client.relations_call == {
        "room_id": "!room:example.com",
        "event_id": "$root",
        "rel_type": RelationshipType.thread,
        "event_type": "m.room.message",
        "direction": MessageDirection.front,
        "limit": 100,
    }

    sent_id = await driver.send_message("!room:example.com", "hi", thread_id="$root")
    assert sent_id == "$sent1"
    assert nio_client.room_send_calls[-1] == (
        "!room:example.com",
        "m.room.message",
        {
            "body": "hi",
            "msgtype": "m.text",
            "m.relates_to": {
                "event_id": "$root",
                "is_falling_back": False,
                "rel_type": "m.thread",
            },
        },
    )

    file_event_id = await driver.send_file("!room:example.com", str(path), thread_id="$root")
    assert file_event_id == "$sent2"
    assert nio_client.upload_call == {
        "content": b"hello",
        "content_type": "text/plain",
        "filename": "report.txt",
        "filesize": 5,
    }
    assert nio_client.room_send_calls[-1] == (
        "!room:example.com",
        "m.room.message",
        {
            "body": "report.txt",
            "filename": "report.txt",
            "info": {"mimetype": "text/plain", "size": 5},
            "msgtype": "m.file",
            "url": "mxc://example.com/report",
            "m.relates_to": {
                "event_id": "$root",
                "is_falling_back": False,
                "rel_type": "m.thread",
            },
        },
    )

    await driver.aclose()
    assert nio_client.closed is True


@pytest.mark.asyncio
async def test_nio_driver_applies_latest_thread_message_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeNioClient.instances.clear()
    monkeypatch.setattr("matrix_mcp.matrix_client.AsyncClient", FakeNioClient)

    driver = NioMatrixDriver(
        MatrixMCPConfig(
            homeserver="https://matrix.example.com/",
            user_id="@alice:example.com",
            device_id="TESTDEVICE",
            access_token="test-token",
        )
    )
    nio_client = FakeNioClient.instances[0]
    nio_client.replacement_events = {
        "$root": [
            edit_event("$root-edit-1", replaces="$root", timestamp_ms=150, body="root draft"),
            edit_event("$root-edit-2", replaces="$root", timestamp_ms=250, body="root final"),
        ],
        "$reply": [
            edit_event(
                "$reply-edit",
                replaces="$reply",
                sender="@bob:example.com",
                timestamp_ms=300,
                body="reply final",
            )
        ],
    }

    thread = await driver.read_thread("!room:example.com", "$root", limit=25)

    assert thread == [
        MatrixEvent(
            event_id="$root",
            sender="@alice:example.com",
            timestamp_ms=100,
            body="root final",
            thread_id=None,
        ),
        MatrixEvent(
            event_id="$reply",
            sender="@bob:example.com",
            timestamp_ms=200,
            body="reply final",
            thread_id="$root",
        ),
    ]
    assert nio_client.replacement_calls == [
        {
            "room_id": "!room:example.com",
            "event_id": "$root",
            "rel_type": RelationshipType.replacement,
            "event_type": "m.room.message",
            "direction": MessageDirection.front,
            "limit": 25,
        },
        {
            "room_id": "!room:example.com",
            "event_id": "$reply",
            "rel_type": RelationshipType.replacement,
            "event_type": "m.room.message",
            "direction": MessageDirection.front,
            "limit": 25,
        },
    ]


@pytest.mark.asyncio
async def test_client_builds_default_driver_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_configs: list[MatrixMCPConfig] = []

    def fake_driver(config: MatrixMCPConfig) -> FakeDriver:
        created_configs.append(config)
        return FakeDriver()

    config = MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id="@alice:example.com",
        device_id="TESTDEVICE",
        access_token="test-token",
    )
    monkeypatch.setattr("matrix_mcp.matrix_client.NioMatrixDriver", fake_driver)
    client = MatrixAPIClient(config=config, id_store=MatrixIdStore(tmp_path / "ids.json"))

    assert await client.whoami() == {
        "user_id": "@alice:example.com",
        "device_id": "CLAUDECODE",
    }
    assert created_configs == [config]


@pytest.mark.asyncio
async def test_list_rooms_reads_joined_room_names() -> None:
    driver = FakeDriver()
    client = MatrixAPIClient(driver=driver)

    rooms = await client.list_rooms()

    assert rooms == [MatrixRoom(room_id="!room:example.com", name="Mind")]


@pytest.mark.asyncio
async def test_client_adds_and_accepts_numeric_refs(tmp_path: Path) -> None:
    driver = FakeDriver()
    id_store = MatrixIdStore(tmp_path / "ids.json")
    client = MatrixAPIClient(driver=driver, id_store=id_store)

    rooms = await client.list_rooms()
    assert rooms == [MatrixRoom(id=1, room_id="!room:example.com", name="Mind")]

    events = await client.read_room_recent(1, limit=10)
    assert events == [
        MatrixEvent(
            id=1,
            event_id="$event1",
            sender="@alice:example.com",
            timestamp_ms=123,
            body="hello",
            thread_id=None,
            thread_ref=None,
        ),
    ]

    id_store.event_ref("$root")
    thread_events = await client.read_thread(1, 2, limit=25)
    assert thread_events[0].id == 2
    assert thread_events[0].thread_ref == 2
    assert thread_events[1].id == 3
    assert thread_events[1].thread_ref == 2

    await client.send_message(1, "hi", thread_id=2)
    assert driver.sent == [("!room:example.com", "hi", "$root")]


@pytest.mark.asyncio
async def test_read_room_recent_normalizes_text_events() -> None:
    driver = FakeDriver()
    client = MatrixAPIClient(driver=driver)

    events = await client.read_room_recent("!room:example.com", limit=10)

    assert events == [
        MatrixEvent(
            event_id="$event1",
            sender="@alice:example.com",
            timestamp_ms=123,
            body="hello",
            thread_id=None,
        ),
    ]


@pytest.mark.asyncio
async def test_read_thread_returns_root_and_replies() -> None:
    driver = FakeDriver()
    client = MatrixAPIClient(driver=driver)

    events = await client.read_thread("!room:example.com", "$root", limit=25)

    assert events == [
        MatrixEvent(
            event_id="$root",
            sender="@alice:example.com",
            timestamp_ms=100,
            body="root",
            thread_id=None,
        ),
        MatrixEvent(
            event_id="$reply",
            sender="@bob:example.com",
            timestamp_ms=200,
            body="reply",
            thread_id="$root",
        ),
    ]


@pytest.mark.asyncio
async def test_send_message_uses_transaction_id_and_optional_thread() -> None:
    driver = FakeDriver()
    client = MatrixAPIClient(driver=driver)

    event_id = await client.send_message("!room:example.com", "hi", thread_id="$root")

    assert event_id == "$sent"
    assert driver.sent == [("!room:example.com", "hi", "$root")]


@pytest.mark.asyncio
async def test_send_file_accepts_optional_thread_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "report.txt"
    path.write_text("hello", encoding="utf-8")
    driver = FakeDriver()
    client = MatrixAPIClient(driver=driver)

    event_id = await client.send_file(
        "!room:example.com",
        str(path),
        thread_id="$root",
        filename="summary.txt",
        content_type="text/plain",
    )

    assert event_id == "$file"
    assert driver.files == [
        ("!room:example.com", str(path), "$root", "summary.txt", "text/plain"),
    ]
