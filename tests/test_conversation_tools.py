from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp import Client, FastMCP

from matrix_mcp.matrix_client import MatrixAPIClient
from matrix_mcp.matrix_events import (
    EventContext,
    HistoryPage,
    ReactionSummary,
    SearchPage,
    ThreadPage,
    TimelineEvent,
)
from matrix_mcp.matrix_media import DownloadedMedia, EventAttachment, UploadedMedia
from matrix_mcp.matrix_moderation import PinnedEvents, PowerLevels
from matrix_mcp.matrix_rooms import (
    DirectRoom,
    InvitationPage,
    ReceiptList,
    SpaceHierarchy,
    UnreadPage,
)
from matrix_mcp.mcp_server import create_mcp_server

if TYPE_CHECKING:
    from matrix_mcp.matrix_client import MatrixDriver

READ_TOOLS = {
    "matrix_read_history",
    "matrix_get_event_context",
    "matrix_search_messages",
    "matrix_list_threads",
    "matrix_get_reactions",
    "matrix_get_read_receipts",
    "matrix_get_space_hierarchy",
    "matrix_get_power_levels",
    "matrix_list_invitations",
    "matrix_get_unread",
    "matrix_download_media",
}
DESTRUCTIVE_TOOLS = {
    "matrix_edit_message",
    "matrix_redact_event",
    "matrix_leave_room",
    "matrix_kick_user",
    "matrix_ban_user",
    "matrix_set_power_level",
}
WRITE_TOOLS = {
    "matrix_reply",
    "matrix_react",
    "matrix_join_room",
    "matrix_create_room",
    "matrix_create_dm",
    "matrix_mark_read",
    "matrix_upload_media",
    "matrix_send_media",
    "matrix_pin_message",
    "matrix_unpin_message",
    "matrix_unban_user",
} | DESTRUCTIVE_TOOLS


class FakeEvents:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    async def history(self, room_id: str, **kwargs: Any) -> HistoryPage:
        self.calls.append(("history", {"room_id": room_id, **kwargs}))
        return HistoryPage(events=[], next_batch="next")

    async def context(self, room_id: str, event_id: str, **kwargs: Any) -> EventContext:
        self.calls.append(("context", {"room_id": room_id, "event_id": event_id, **kwargs}))
        return EventContext(
            event=TimelineEvent(
                event_id=event_id,
                sender="@alice:example.com",
                type="m.room.message",
                msgtype="m.text",
                body="hello",
            ),
            events_before=[],
            events_after=[],
        )

    async def reply(self, room_id: str, event_id: str, body: str, **kwargs: Any) -> str:
        self.calls.append(
            ("reply", {"room_id": room_id, "event_id": event_id, "body": body, **kwargs})
        )
        return "$reply"

    async def react(self, room_id: str, event_id: str, key: str, **kwargs: Any) -> str:
        self.calls.append(
            ("react", {"room_id": room_id, "event_id": event_id, "key": key, **kwargs})
        )
        return "$reaction"

    async def edit(self, room_id: str, event_id: str, body: str, **kwargs: Any) -> str:
        self.calls.append(
            ("edit", {"room_id": room_id, "event_id": event_id, "body": body, **kwargs})
        )
        return "$edit"

    async def redact(self, room_id: str, event_id: str, **kwargs: Any) -> str:
        self.calls.append(("redact", {"room_id": room_id, "event_id": event_id, **kwargs}))
        return "$redaction"

    async def attachment(self, room_id: str, event_id: str) -> EventAttachment:
        self.calls.append(("attachment", {"room_id": room_id, "event_id": event_id}))
        return EventAttachment(url="mxc://example.com/media", mimetype="text/plain")

    async def search(self, search_term: str, **kwargs: Any) -> SearchPage:
        self.calls.append(("search", {"search_term": search_term, **kwargs}))
        return SearchPage(results=[])

    async def threads(self, room_id: str, **kwargs: Any) -> ThreadPage:
        self.calls.append(("threads", {"room_id": room_id, **kwargs}))
        return ThreadPage(threads=[])

    async def reactions(self, room_id: str, event_id: str, **kwargs: Any) -> ReactionSummary:
        self.calls.append(("reactions", {"room_id": room_id, "event_id": event_id, **kwargs}))
        return ReactionSummary(event_id=event_id, reactions=[])


class FakeRooms:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    async def invitations(self, **kwargs: Any) -> InvitationPage:
        self.calls.append(("invitations", kwargs))
        return InvitationPage(rooms=[], total=0, next_offset=None)

    async def join(self, room_id_or_alias: str) -> str:
        self.calls.append(("join", {"room_id_or_alias": room_id_or_alias}))
        return "!joined"

    async def leave(self, room_id: str, **kwargs: Any) -> None:
        self.calls.append(("leave", {"room_id": room_id, **kwargs}))

    async def create(self, **kwargs: Any) -> str:
        self.calls.append(("create", kwargs))
        return "!created"

    async def unread(self, **kwargs: Any) -> UnreadPage:
        self.calls.append(("unread", kwargs))
        return UnreadPage(rooms=[], total=0, next_offset=None)

    async def mark_read(self, room_id: str, event_id: str, **kwargs: Any) -> None:
        self.calls.append(("mark_read", {"room_id": room_id, "event_id": event_id, **kwargs}))

    async def create_dm(self, user_id: str, **kwargs: Any) -> DirectRoom:
        self.calls.append(("create_dm", {"user_id": user_id, **kwargs}))
        return DirectRoom(room_id="!dm", created=True)

    async def receipts(self, room_id: str, **kwargs: Any) -> ReceiptList:
        self.calls.append(("receipts", {"room_id": room_id, **kwargs}))
        return ReceiptList(receipts=[], total=0)

    async def hierarchy(self, space_id: str, **kwargs: Any) -> SpaceHierarchy:
        self.calls.append(("hierarchy", {"space_id": space_id, **kwargs}))
        return SpaceHierarchy(rooms=[])


class FakeModeration:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    async def kick(self, room_id: str, user_id: str, **kwargs: Any) -> None:
        self.calls.append(("kick", {"room_id": room_id, "user_id": user_id, **kwargs}))

    async def ban(self, room_id: str, user_id: str, **kwargs: Any) -> None:
        self.calls.append(("ban", {"room_id": room_id, "user_id": user_id, **kwargs}))

    async def unban(self, room_id: str, user_id: str, **kwargs: Any) -> None:
        self.calls.append(("unban", {"room_id": room_id, "user_id": user_id, **kwargs}))

    async def power_levels(self, room_id: str) -> PowerLevels:
        self.calls.append(("power_levels", {"room_id": room_id}))
        return PowerLevels(own_level=100)

    async def set_power_level(self, room_id: str, user_id: str, level: int | None) -> str:
        self.calls.append(
            ("set_power_level", {"room_id": room_id, "user_id": user_id, "level": level})
        )
        return "$levels"

    async def pin(self, room_id: str, event_id: str) -> PinnedEvents:
        self.calls.append(("pin", {"room_id": room_id, "event_id": event_id}))
        return PinnedEvents(pinned=[event_id], changed=True)

    async def unpin(self, room_id: str, event_id: str) -> PinnedEvents:
        self.calls.append(("unpin", {"room_id": room_id, "event_id": event_id}))
        return PinnedEvents(pinned=[], changed=True)


class FakeMedia:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    async def upload(self, data_base64: str, filename: str, **kwargs: Any) -> UploadedMedia:
        self.calls.append(("upload", {"data_base64": data_base64, "filename": filename, **kwargs}))
        return UploadedMedia(
            content_uri="mxc://example.com/media",
            filename=filename,
            content_type=kwargs["content_type"],
            size=1,
        )

    async def download(
        self, media_url: str, *, attachment: EventAttachment | None = None
    ) -> DownloadedMedia:
        self.calls.append(("download", {"media_url": media_url, "attachment": attachment}))
        return DownloadedMedia(
            media_url=media_url,
            content_type="text/plain",
            size=1,
            data_base64="eA==",
        )

    async def send(self, room_id: str, media_url: str, filename: str, **kwargs: Any) -> str:
        self.calls.append(
            (
                "send_media",
                {"room_id": room_id, "media_url": media_url, "filename": filename, **kwargs},
            )
        )
        return "$media"


class GroupedDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events = FakeEvents(self.calls)
        self.rooms = FakeRooms(self.calls)
        self.media = FakeMedia(self.calls)
        self.moderation = FakeModeration(self.calls)


def grouped_server(driver: GroupedDriver) -> FastMCP:
    def factory() -> MatrixAPIClient:
        return MatrixAPIClient(driver=cast("MatrixDriver", driver))

    return create_mcp_server(factory)


async def test_conversation_tools_expose_all_five_workflows() -> None:
    async with Client(create_mcp_server()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names >= READ_TOOLS | WRITE_TOOLS


async def test_conversation_tools_annotate_reads_and_mutations() -> None:
    async with Client(create_mcp_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in READ_TOOLS:
        assert name in tools
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is True
    for name in WRITE_TOOLS:
        assert name in tools
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is (name in DESTRUCTIVE_TOOLS)


@pytest.mark.parametrize("name", ["matrix_read_history", "matrix_get_event_context"])
async def test_new_history_tools_require_raw_room_ids(name: str) -> None:
    async with Client(create_mcp_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert name in tools
    schema = tools[name].inputSchema["properties"]["room_id"]
    assert schema["type"] == "string"
    assert schema.get("pattern")


async def test_media_arguments_cannot_choose_server_paths_or_urls() -> None:
    async with Client(create_mcp_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in ("matrix_upload_media", "matrix_download_media", "matrix_send_media"):
        assert name in tools
        properties = tools[name].inputSchema["properties"]
        assert not {"file_path", "homeserver", "access_token", "http_url"} & properties.keys()


async def test_send_media_size_schema_uses_json_safe_integer_bound() -> None:
    async with Client(create_mcp_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    size_schema = tools["matrix_send_media"].inputSchema["properties"]["size"]
    integer_schema = next(item for item in size_schema["anyOf"] if item.get("type") == "integer")
    assert integer_schema["minimum"] == 0
    assert integer_schema["maximum"] == 2**53 - 1


@pytest.mark.parametrize(
    "media_url",
    [
        "mxc://example..com/media",
        "mxc://./media",
        "mxc://-example.com/media",
        "mxc://example-.com/media",
        "mxc://éxample.com/media",
    ],
)
async def test_media_url_schema_rejects_invalid_server_authorities(media_url: str) -> None:
    driver = GroupedDriver()
    async with Client(grouped_server(driver)) as client:
        result = await client.call_tool(
            "matrix_download_media",
            {"media_url": media_url},
            raise_on_error=False,
        )
    assert result.is_error
    assert driver.calls == []


@pytest.mark.parametrize("media_url", ["MXC://example.com/media", "MxC://example.com/media"])
async def test_media_url_schema_accepts_case_insensitive_scheme(media_url: str) -> None:
    driver = GroupedDriver()
    async with Client(grouped_server(driver)) as client:
        result = await client.call_tool("matrix_download_media", {"media_url": media_url})

    assert not result.is_error
    assert driver.calls == [("download", {"media_url": media_url, "attachment": None})]


async def test_history_dispatch_accepts_domainless_room_id_without_marking_read() -> None:
    driver = GroupedDriver()
    async with Client(grouped_server(driver)) as client:
        result = await client.call_tool(
            "matrix_read_history",
            {"room_id": "!v12hash", "limit": 7, "before": "cursor"},
        )
    assert not result.is_error
    assert driver.calls == [("history", {"room_id": "!v12hash", "limit": 7, "before": "cursor"})]


@pytest.mark.parametrize(
    ("name", "arguments", "expected_call"),
    [
        (
            "matrix_get_event_context",
            {"room_id": "!room:example.com", "event_id": "$event", "limit": 9},
            (
                "context",
                {"room_id": "!room:example.com", "event_id": "$event", "limit": 9},
            ),
        ),
        (
            "matrix_reply",
            {"room_id": "!room:example.com", "event_id": "$event", "body": "reply"},
            (
                "reply",
                {
                    "room_id": "!room:example.com",
                    "event_id": "$event",
                    "body": "reply",
                    "transaction_id": None,
                },
            ),
        ),
        (
            "matrix_react",
            {"room_id": "!room:example.com", "event_id": "$event", "key": "👍"},
            (
                "react",
                {
                    "room_id": "!room:example.com",
                    "event_id": "$event",
                    "key": "👍",
                    "transaction_id": None,
                },
            ),
        ),
        (
            "matrix_edit_message",
            {"room_id": "!room:example.com", "event_id": "$event", "body": "fixed"},
            (
                "edit",
                {
                    "room_id": "!room:example.com",
                    "event_id": "$event",
                    "body": "fixed",
                    "transaction_id": None,
                },
            ),
        ),
        (
            "matrix_redact_event",
            {"room_id": "!room:example.com", "event_id": "$event", "reason": "duplicate"},
            (
                "redact",
                {
                    "room_id": "!room:example.com",
                    "event_id": "$event",
                    "reason": "duplicate",
                    "transaction_id": None,
                },
            ),
        ),
        (
            "matrix_list_invitations",
            {"limit": 12, "offset": 3},
            ("invitations", {"limit": 12, "offset": 3}),
        ),
        (
            "matrix_join_room",
            {"room_id_or_alias": "#general:example.com"},
            ("join", {"room_id_or_alias": "#general:example.com"}),
        ),
        (
            "matrix_leave_room",
            {"room_id": "!v12hash", "reason": "done"},
            ("leave", {"room_id": "!v12hash", "reason": "done"}),
        ),
        (
            "matrix_create_room",
            {
                "name": "Project",
                "topic": "Planning",
                "invite": ["@bob:example.com"],
            },
            (
                "create",
                {
                    "name": "Project",
                    "topic": "Planning",
                    "invite": ["@bob:example.com"],
                },
            ),
        ),
        (
            "matrix_search_messages",
            {"search_term": "release date", "room_id": "!v12hash", "limit": 5},
            (
                "search",
                {
                    "search_term": "release date",
                    "room_id": "!v12hash",
                    "limit": 5,
                    "order_by": "recent",
                    "next_batch": None,
                },
            ),
        ),
        (
            "matrix_search_messages",
            {"search_term": "plan", "order_by": "rank", "next_batch": "page-2"},
            (
                "search",
                {
                    "search_term": "plan",
                    "room_id": None,
                    "limit": 10,
                    "order_by": "rank",
                    "next_batch": "page-2",
                },
            ),
        ),
        (
            "matrix_list_threads",
            {"room_id": "!room:example.com", "include": "participated", "before": "older"},
            (
                "threads",
                {
                    "room_id": "!room:example.com",
                    "include": "participated",
                    "limit": 20,
                    "before": "older",
                },
            ),
        ),
        (
            "matrix_get_reactions",
            {"room_id": "!room:example.com", "event_id": "$event", "limit": 50},
            ("reactions", {"room_id": "!room:example.com", "event_id": "$event", "limit": 50}),
        ),
        (
            "matrix_get_read_receipts",
            {"room_id": "!room:example.com", "event_id": "$event"},
            ("receipts", {"room_id": "!room:example.com", "event_id": "$event", "limit": 50}),
        ),
        (
            "matrix_get_space_hierarchy",
            {"space_id": "!space:example.com", "max_depth": 2, "next_batch": "more"},
            (
                "hierarchy",
                {
                    "space_id": "!space:example.com",
                    "limit": 50,
                    "max_depth": 2,
                    "next_batch": "more",
                },
            ),
        ),
        (
            "matrix_get_power_levels",
            {"room_id": "!room:example.com"},
            ("power_levels", {"room_id": "!room:example.com"}),
        ),
        (
            "matrix_create_dm",
            {"user_id": "@bob:example.com", "encrypted": True},
            ("create_dm", {"user_id": "@bob:example.com", "encrypted": True}),
        ),
        (
            "matrix_pin_message",
            {"room_id": "!room:example.com", "event_id": "$event"},
            ("pin", {"room_id": "!room:example.com", "event_id": "$event"}),
        ),
        (
            "matrix_unpin_message",
            {"room_id": "!room:example.com", "event_id": "$event"},
            ("unpin", {"room_id": "!room:example.com", "event_id": "$event"}),
        ),
        (
            "matrix_kick_user",
            {"room_id": "!room:example.com", "user_id": "@bob:example.com", "reason": "spam"},
            (
                "kick",
                {"room_id": "!room:example.com", "user_id": "@bob:example.com", "reason": "spam"},
            ),
        ),
        (
            "matrix_ban_user",
            {"room_id": "!room:example.com", "user_id": "@bob:example.com"},
            (
                "ban",
                {"room_id": "!room:example.com", "user_id": "@bob:example.com", "reason": None},
            ),
        ),
        (
            "matrix_unban_user",
            {"room_id": "!room:example.com", "user_id": "@bob:example.com"},
            (
                "unban",
                {"room_id": "!room:example.com", "user_id": "@bob:example.com", "reason": None},
            ),
        ),
        (
            "matrix_set_power_level",
            {"room_id": "!room:example.com", "user_id": "@bob:example.com", "level": 50},
            (
                "set_power_level",
                {"room_id": "!room:example.com", "user_id": "@bob:example.com", "level": 50},
            ),
        ),
        (
            "matrix_set_power_level",
            {"room_id": "!room:example.com", "user_id": "@bob:example.com", "level": None},
            (
                "set_power_level",
                {"room_id": "!room:example.com", "user_id": "@bob:example.com", "level": None},
            ),
        ),
        (
            "matrix_get_unread",
            {"limit": 8, "offset": 2, "timeline_limit": 6},
            (
                "unread",
                {"limit": 8, "offset": 2, "timeline_limit": 6},
            ),
        ),
        (
            "matrix_mark_read",
            {"room_id": "!v12hash", "event_id": "$event", "public_receipt": True},
            (
                "mark_read",
                {"room_id": "!v12hash", "event_id": "$event", "public_receipt": True},
            ),
        ),
        (
            "matrix_upload_media",
            {"data_base64": "eA==", "filename": "x.txt", "content_type": "text/plain"},
            (
                "upload",
                {"data_base64": "eA==", "filename": "x.txt", "content_type": "text/plain"},
            ),
        ),
        (
            "matrix_download_media",
            {"media_url": "mxc://example.com/media"},
            ("download", {"media_url": "mxc://example.com/media", "attachment": None}),
        ),
        (
            "matrix_send_media",
            {
                "room_id": "!v12hash",
                "media_url": "mxc://example.com/media",
                "filename": "x.txt",
                "content_type": "text/plain",
                "size": 1,
                "thread_id": "$root",
                "transaction_id": "tx-1",
            },
            (
                "send_media",
                {
                    "room_id": "!v12hash",
                    "media_url": "mxc://example.com/media",
                    "filename": "x.txt",
                    "content_type": "text/plain",
                    "size": 1,
                    "thread_id": "$root",
                    "transaction_id": "tx-1",
                },
            ),
        ),
    ],
)
async def test_conversation_tool_dispatches_exact_arguments(
    name: str, arguments: dict[str, Any], expected_call: tuple[str, dict[str, Any]]
) -> None:
    driver = GroupedDriver()
    async with Client(grouped_server(driver)) as client:
        result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert driver.calls == [expected_call]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("matrix_read_history", {"room_id": "room", "limit": 20}),
        ("matrix_get_event_context", {"room_id": "!room", "event_id": "event"}),
        ("matrix_read_history", {"room_id": "!room:example.com", "limit": 0}),
        (
            "matrix_get_event_context",
            {"room_id": "!room:example.com", "event_id": "$e", "limit": 51},
        ),
        ("matrix_get_unread", {"limit": 101}),
        ("matrix_search_messages", {"search_term": ""}),
        ("matrix_search_messages", {"search_term": "x", "limit": 51}),
        ("matrix_search_messages", {"search_term": "x", "order_by": "oldest"}),
        ("matrix_list_threads", {"room_id": "!room:example.com", "include": "mine"}),
        ("matrix_get_reactions", {"room_id": "!room:example.com", "event_id": "event"}),
        ("matrix_get_space_hierarchy", {"space_id": "!space:example.com", "max_depth": 6}),
        ("matrix_create_dm", {"user_id": "bob"}),
        ("matrix_kick_user", {"room_id": "!room:example.com", "user_id": "@bob"}),
        (
            "matrix_set_power_level",
            {"room_id": "!room:example.com", "user_id": "@bob:example.com", "level": 2**53},
        ),
        ("matrix_set_power_level", {"room_id": "!room:example.com", "user_id": "@bob:example.com"}),
        (
            "matrix_send_media",
            {
                "room_id": "!room:example.com",
                "media_url": "https://example.com/x",
                "filename": "x",
            },
        ),
    ],
)
async def test_conversation_tools_reject_malformed_inputs(
    name: str, arguments: dict[str, Any]
) -> None:
    async with Client(create_mcp_server()) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
    assert result.is_error


async def test_conversation_tool_closes_matrix_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = GroupedDriver()
    closed: list[MatrixAPIClient] = []

    async def track_close(client: MatrixAPIClient) -> None:
        closed.append(client)

    monkeypatch.setattr(MatrixAPIClient, "aclose", track_close)
    async with Client(grouped_server(driver)) as client:
        await client.call_tool("matrix_read_history", {"room_id": "!v12hash"})
    assert len(closed) == 1


async def test_download_media_resolves_the_event_attachment_first() -> None:
    driver = GroupedDriver()
    async with Client(grouped_server(driver)) as client:
        result = await client.call_tool(
            "matrix_download_media",
            {
                "media_url": "mxc://example.com/media",
                "room_id": "!room:example.com",
                "event_id": "$file",
            },
        )
    assert not result.is_error
    assert driver.calls == [
        ("attachment", {"room_id": "!room:example.com", "event_id": "$file"}),
        (
            "download",
            {
                "media_url": "mxc://example.com/media",
                "attachment": EventAttachment(url="mxc://example.com/media", mimetype="text/plain"),
            },
        ),
    ]


@pytest.mark.parametrize("missing", ["room_id", "event_id"])
async def test_download_media_needs_room_and_event_together(missing: str) -> None:
    driver = GroupedDriver()
    arguments = {
        "media_url": "mxc://example.com/media",
        "room_id": "!room:example.com",
        "event_id": "$file",
    }
    del arguments[missing]
    async with Client(grouped_server(driver)) as client:
        result = await client.call_tool("matrix_download_media", arguments, raise_on_error=False)
    assert result.is_error
    assert driver.calls == []


async def test_tool_guidance_steers_encrypted_file_sharing() -> None:
    async with Client(create_mcp_server()) as client:
        tools = {tool.name: tool.description or "" for tool in await client.list_tools()}
        instructions = client.initialize_result.instructions or ""

    assert "unencrypted" in tools["matrix_upload_media"]
    assert "file_path" in tools["matrix_upload_media"]
    assert "encrypted" in tools["matrix_send_message"]
    assert "file_path" in instructions
    assert "missing room key" in instructions
