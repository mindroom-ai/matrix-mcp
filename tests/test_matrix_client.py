from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from matrix_mcp.matrix_client import MatrixAPIClient, MatrixEvent, MatrixRoom

if TYPE_CHECKING:
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


@pytest.mark.asyncio
async def test_list_rooms_reads_joined_room_names() -> None:
    driver = FakeDriver()
    client = MatrixAPIClient(driver=driver)

    rooms = await client.list_rooms()

    assert rooms == [MatrixRoom(room_id="!room:example.com", name="Mind")]


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
