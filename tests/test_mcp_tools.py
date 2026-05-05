from __future__ import annotations

import pytest

from matrix_mcp.matrix_client import MatrixEvent, MatrixRoom
from matrix_mcp.mcp_server import MatrixMCPTools


class FakeMatrixClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str | int, str, str | int | None]] = []
        self.files: list[tuple[str | int, str, str | int | None, str | None, str | None]] = []

    async def whoami(self) -> dict[str, str | None]:
        return {"user_id": "@alice:example.com", "device_id": "CLAUDECODE"}

    async def list_rooms(self) -> list[MatrixRoom]:
        return [MatrixRoom(room_id="!mind:example.com", name="Mind")]

    async def read_room_recent(self, room_id: str | int, *, limit: int = 20) -> list[MatrixEvent]:
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
        self, room_id: str | int, thread_id: str | int, *, limit: int = 50
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

    async def send_message(
        self,
        room_id: str | int,
        body: str,
        *,
        thread_id: str | int | None = None,
    ) -> str:
        self.sent.append((room_id, body, thread_id))
        return "$sent"

    async def send_file(
        self,
        room_id: str | int,
        file_path: str,
        *,
        thread_id: str | int | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        self.files.append((room_id, file_path, thread_id, filename, content_type))
        return "$file"


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
    assert matrix.sent == [("!mind:example.com", "hi", "$root")]


@pytest.mark.asyncio
async def test_file_tools_send_room_and_thread_attachments() -> None:
    matrix = FakeMatrixClient()
    tools = MatrixMCPTools(client_factory=lambda: matrix)

    report_path = "workspace/report.txt"
    thread_report_path = "workspace/thread-report.txt"

    assert await tools.matrix_send_message(
        "!mind:example.com",
        file_path=report_path,
        filename="report.txt",
        content_type="text/plain",
    ) == {"event_id": "$file"}
    assert await tools.matrix_send_message(
        "!mind:example.com",
        thread_id="$root",
        file_path=thread_report_path,
        filename="thread-report.txt",
        content_type="text/plain",
    ) == {"event_id": "$file"}
    assert matrix.files == [
        ("!mind:example.com", report_path, None, "report.txt", "text/plain"),
        ("!mind:example.com", thread_report_path, "$root", "thread-report.txt", "text/plain"),
    ]


@pytest.mark.asyncio
async def test_send_message_requires_text_or_attachment() -> None:
    matrix = FakeMatrixClient()
    tools = MatrixMCPTools(client_factory=lambda: matrix)

    with pytest.raises(ValueError, match="body or file_path"):
        await tools.matrix_send_message("!mind:example.com")
