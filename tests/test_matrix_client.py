from __future__ import annotations

import pytest

from matrix_mcp.matrix_client import MatrixAPIClient, MatrixEvent, MatrixRoom


class FakeDriver:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
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

    async def send_message(self, room_id: str, body: str, *, thread_id: str | None = None) -> str:
        self.sent.append((room_id, body, thread_id))
        return "$sent"


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
async def test_send_message_uses_transaction_id_and_optional_thread() -> None:
    driver = FakeDriver()
    client = MatrixAPIClient(driver=driver)

    event_id = await client.send_message("!room:example.com", "hi", thread_id="$root")

    assert event_id == "$sent"
    assert driver.sent == [("!room:example.com", "hi", "$root")]
