from __future__ import annotations

import pytest

from matrix_mcp.matrix_client import MatrixEvent, MatrixRoom
from matrix_mcp.mcp_server import MatrixMCPTools


class FakeMatrixClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def whoami(self) -> dict[str, str | None]:
        return {"user_id": "@alice:example.com", "device_id": "CLAUDECODE"}

    async def list_rooms(self) -> list[MatrixRoom]:
        return [MatrixRoom(room_id="!mind:example.com", name="Mind")]

    async def read_room_recent(self, room_id: str, *, limit: int = 20) -> list[MatrixEvent]:
        assert room_id == "!mind:example.com"
        assert limit == 5
        return [
            MatrixEvent(
                event_id="$event",
                sender="@alice:example.com",
                timestamp_ms=123,
                body="hello",
                thread_id=None,
            ),
        ]

    async def read_thread(
        self, room_id: str, thread_id: str, *, limit: int = 50
    ) -> list[MatrixEvent]:
        assert room_id == "!mind:example.com"
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


@pytest.mark.asyncio
async def test_tools_return_pydantic_models() -> None:
    matrix = FakeMatrixClient()
    tools = MatrixMCPTools(client_factory=lambda: matrix)

    assert await tools.matrix_whoami() == {
        "user_id": "@alice:example.com",
        "device_id": "CLAUDECODE",
    }
    assert await tools.matrix_list_rooms() == [MatrixRoom(room_id="!mind:example.com", name="Mind")]
    assert await tools.matrix_read_room_recent("!mind:example.com", limit=5) == [
        MatrixEvent(
            event_id="$event",
            sender="@alice:example.com",
            timestamp_ms=123,
            body="hello",
            thread_id=None,
        ),
    ]
    assert await tools.matrix_read_thread("!mind:example.com", "$root", limit=25) == [
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
    assert await tools.matrix_send_message("!mind:example.com", "hi", thread_id="$root") == {
        "event_id": "$sent"
    }
    assert await tools.matrix_reply_thread("!mind:example.com", "$root", "thread reply") == {
        "event_id": "$sent"
    }
    assert matrix.sent == [
        ("!mind:example.com", "hi", "$root"),
        ("!mind:example.com", "thread reply", "$root"),
    ]
