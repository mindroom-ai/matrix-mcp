from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.matrix_events import MatrixEvents
from matrix_mcp.matrix_http import MatrixHTTP

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


ROOM = "!room:example.com"
ROOM_PATH = "/_matrix/client/v3/rooms/!room:example.com"
RELATIONS_PATH = "/_matrix/client/v1/rooms/!room:example.com/relations"


def message(  # noqa: PLR0913
    event_id: str,
    *,
    sender: str = "@alice:example.com",
    body: str = "hello",
    msgtype: str = "m.text",
    timestamp: int = 100,
    relates_to: dict[str, object] | None = None,
    unsigned: dict[str, object] | None = None,
) -> dict[str, Any]:
    content: dict[str, object] = {"msgtype": msgtype, "body": body}
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": content,
        "unsigned": unsigned or {},
    }


@dataclass
class MatrixEndpoint:
    responses: dict[tuple[str, str], list[tuple[object, int]]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def respond(self, method: str, path: str, data: object, *, status: int = 200) -> None:
        self.responses.setdefault((method, path), []).append((data, status))

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "body": body,
            }
        )
        queued = self.responses.get((request.method, request.path))
        if queued:
            data, status = queued.pop(0)
            return web.json_response(data, status=status)
        if request.path.endswith("/state/m.room.encryption"):
            return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)
        if "/relations/" in request.path:
            return web.json_response({"chunk": []})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)


@pytest.fixture
async def matrix() -> AsyncIterator[tuple[MatrixEvents, MatrixEndpoint]]:
    endpoint = MatrixEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        config = MatrixMCPConfig(
            homeserver=str(server.make_url("/")),
            user_id="@alice:example.com",
            device_id="TESTDEVICE",
            access_token="test-token",
        )
        yield MatrixEvents(MatrixHTTP(config)), endpoint


async def test_history_preserves_cursor_and_attachment(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        {
            "chunk": [
                {
                    "event_id": "$file",
                    "sender": "@alice:example.com",
                    "origin_server_ts": 123,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.file",
                        "body": "report.txt",
                        "filename": "source.txt",
                        "url": "mxc://example.com/file",
                        "info": {"mimetype": "text/plain", "size": 42},
                    },
                }
            ],
            "end": "older",
        },
    )

    page = await events.history(ROOM, limit=1, before="opaque+/cursor")

    assert page.next_batch == "older"
    assert page.edit_resolution_truncated is False
    assert page.events[0].body == "report.txt"
    assert page.events[0].media is not None
    assert page.events[0].media.model_dump() == {
        "url": "mxc://example.com/file",
        "filename": "source.txt",
        "mimetype": "text/plain",
        "size": 42,
        "width": None,
        "height": None,
        "duration_ms": None,
    }
    assert endpoint.requests[0]["query"] == {
        "dir": "b",
        "limit": "1",
        "from": "opaque+/cursor",
    }


async def test_history_rejects_server_page_over_requested_limit(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/messages",
        {"chunk": [message("$one"), message("$two")], "end": "after-both"},
    )

    with pytest.raises(RuntimeError, match="exceeded requested limit"):
        await events.history(ROOM, limit=1)

    assert len(endpoint.requests) == 1


async def test_history_applies_valid_bundled_media_edit_and_preserves_relationships(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    original = message(
        "$original",
        body="old.txt",
        msgtype="m.file",
        relates_to={
            "rel_type": "m.thread",
            "event_id": "$root",
            "m.in_reply_to": {"event_id": "$previous"},
        },
        unsigned={
            "m.relations": {
                "m.replace": {
                    "event_id": "$edit",
                    "sender": "@alice:example.com",
                    "origin_server_ts": 200,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.file",
                        "body": "* new.txt",
                        "m.new_content": {
                            "msgtype": "m.file",
                            "body": "new.txt",
                            "url": "mxc://example.com/new",
                            "info": {"mimetype": "text/plain", "size": 9},
                        },
                        "m.relates_to": {
                            "rel_type": "m.replace",
                            "event_id": "$original",
                        },
                    },
                }
            }
        },
    )
    endpoint.respond("GET", f"{ROOM_PATH}/messages", {"chunk": [original]})

    page = await events.history(ROOM)

    event = page.events[0]
    assert event.event_id == "$original"
    assert event.edited is True
    assert event.body == "new.txt"
    assert event.thread_id == "$root"
    assert event.reply_to == "$previous"
    assert event.media is not None
    assert event.media.url == "mxc://example.com/new"
    assert len(endpoint.requests) == 1


async def test_history_ignores_forged_and_redacted_replacements_and_discloses_truncation(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    original = message(
        "$original",
        unsigned={"m.relations": {"m.replace": {"sender": "@mallory:example.com"}}},
    )
    endpoint.respond("GET", f"{ROOM_PATH}/messages", {"chunk": [original]})
    relation_path = f"{RELATIONS_PATH}/$original/m.replace/m.room.message"
    forged = message(
        "$forged",
        sender="@mallory:example.com",
        body="* forged",
        timestamp=300,
        relates_to={"rel_type": "m.replace", "event_id": "$original"},
    )
    forged["content"]["m.new_content"] = {"msgtype": "m.text", "body": "forged"}
    redacted = message(
        "$redacted-edit",
        body="* erased",
        timestamp=250,
        relates_to={"rel_type": "m.replace", "event_id": "$original"},
        unsigned={"redacted_because": {"event_id": "$redaction"}},
    )
    redacted["content"]["m.new_content"] = {"msgtype": "m.text", "body": "erased"}
    endpoint.respond(
        "GET",
        relation_path,
        {"chunk": [forged, redacted], "next_batch": "more"},
    )
    endpoint.respond(
        "GET",
        relation_path,
        {
            "chunk": [
                {
                    **message(
                        "$valid",
                        body="* corrected",
                        timestamp=200,
                        relates_to={"rel_type": "m.replace", "event_id": "$original"},
                    ),
                    "content": {
                        "msgtype": "m.text",
                        "body": "* corrected",
                        "m.new_content": {"msgtype": "m.text", "body": "corrected"},
                        "m.relates_to": {
                            "rel_type": "m.replace",
                            "event_id": "$original",
                        },
                    },
                }
            ],
            "next_batch": "still-more",
        },
    )

    page = await events.history(ROOM)

    assert page.events[0].body == "corrected"
    assert page.events[0].edited is True
    assert page.edit_resolution_truncated is True
    relation_requests = [
        request for request in endpoint.requests if "/relations/" in request["path"]
    ]
    assert relation_requests[1]["query"]["from"] == "more"


async def test_history_keeps_redacted_placeholder_and_hides_replacement_events(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    redacted = message(
        "$redacted",
        body="",
        unsigned={"redacted_because": {"event_id": "$redaction"}},
    )
    redacted["content"] = {}
    replacement = message(
        "$edit",
        relates_to={"rel_type": "m.replace", "event_id": "$redacted"},
    )
    endpoint.respond(
        "GET", f"{ROOM_PATH}/messages", {"chunk": [replacement, redacted], "end": "older"}
    )

    page = await events.history(ROOM)

    assert len(page.events) == 1
    assert page.events[0].event_id == "$redacted"
    assert page.events[0].redacted is True
    assert page.events[0].body is None


async def test_context_preserves_tokens_and_expands_each_section(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/context/$target",
        {
            "event": message("$target", body="target"),
            "events_before": [message("$before", body="before")],
            "events_after": [message("$after", body="after")],
            "start": "newer",
            "end": "older",
        },
    )

    context = await events.context(ROOM, "$target", limit=500)

    assert context.event.body == "target"
    assert [event.body for event in context.events_before] == ["before"]
    assert [event.body for event in context.events_after] == ["after"]
    assert context.start == "newer"
    assert context.end == "older"
    assert context.edit_resolution_truncated is False
    assert endpoint.requests[0]["query"] == {"limit": "50"}


async def test_context_rejects_surrounding_events_over_requested_limit(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/context/$target",
        {
            "event": message("$target"),
            "events_before": [message("$before")],
            "events_after": [message("$after")],
            "start": "newer",
            "end": "older",
        },
    )

    with pytest.raises(RuntimeError, match="exceeded requested limit"):
        await events.context(ROOM, "$target", limit=1)

    assert len(endpoint.requests) == 1


async def test_reply_in_thread_uses_target_and_thread_root(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/event/$target",
        message(
            "$target",
            relates_to={"rel_type": "m.thread", "event_id": "$root"},
        ),
    )
    endpoint.respond(
        "PUT",
        f"{ROOM_PATH}/send/m.room.message/reply-txn",
        {"event_id": "$reply"},
    )

    event_id = await events.reply(ROOM, "$target", "answer", transaction_id="reply-txn")

    assert event_id == "$reply"
    assert endpoint.requests[-1] == {
        "method": "PUT",
        "path": f"{ROOM_PATH}/send/m.room.message/reply-txn",
        "query": {},
        "body": {
            "msgtype": "m.text",
            "body": "answer",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$root",
                "is_falling_back": False,
                "m.in_reply_to": {"event_id": "$target"},
            },
        },
    }


async def test_react_sends_exact_annotation_payload(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond("PUT", f"{ROOM_PATH}/send/m.reaction/react-txn", {"event_id": "$reaction"})

    event_id = await events.react(ROOM, "$target", "👍", transaction_id="react-txn")

    assert event_id == "$reaction"
    assert endpoint.requests[-1]["body"] == {
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": "$target",
            "key": "👍",
        }
    }


async def test_edit_own_text_preserves_msgtype_and_uses_replacement_payload(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond("GET", f"{ROOM_PATH}/event/$target", message("$target", msgtype="m.notice"))
    endpoint.respond("PUT", f"{ROOM_PATH}/send/m.room.message/edit-txn", {"event_id": "$edit"})

    event_id = await events.edit(ROOM, "$target", "updated", transaction_id="edit-txn")

    assert event_id == "$edit"
    assert endpoint.requests[-1]["body"] == {
        "msgtype": "m.notice",
        "body": "* updated",
        "m.new_content": {"msgtype": "m.notice", "body": "updated"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
    }


async def test_redact_checks_sender_and_sends_reason_without_encryption_lookup(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/event/$reaction",
        {
            "event_id": "$reaction",
            "sender": "@alice:example.com",
            "origin_server_ts": 100,
            "type": "m.reaction",
            "content": {},
        },
    )
    endpoint.respond("PUT", f"{ROOM_PATH}/redact/$reaction/redact-txn", {"event_id": "$redaction"})

    event_id = await events.redact(
        ROOM, "$reaction", reason="duplicate", transaction_id="redact-txn"
    )

    assert event_id == "$redaction"
    assert endpoint.requests[-1]["body"] == {"reason": "duplicate"}
    assert all(not request["path"].endswith("m.room.encryption") for request in endpoint.requests)


@pytest.mark.parametrize("action", ["edit", "redact"])
async def test_own_event_actions_reject_another_sender(
    matrix: tuple[MatrixEvents, MatrixEndpoint], action: str
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/event/$target",
        message("$target", sender="@bob:example.com"),
    )

    operation = (
        events.edit(ROOM, "$target", "updated")
        if action == "edit"
        else events.redact(ROOM, "$target")
    )
    with pytest.raises(ValueError, match="connected user"):
        await operation

    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("msgtype", ["m.file", "m.image", "m.audio"])
async def test_edit_rejects_non_text_message_types(
    matrix: tuple[MatrixEvents, MatrixEndpoint], msgtype: str
) -> None:
    events, endpoint = matrix
    endpoint.respond("GET", f"{ROOM_PATH}/event/$target", message("$target", msgtype=msgtype))

    with pytest.raises(ValueError, match="text, notice, or emote"):
        await events.edit(ROOM, "$target", "updated")

    assert len(endpoint.requests) == 1


async def test_edit_rejects_redacted_message(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    target = message("$target", unsigned={"redacted_because": {"event_id": "$redaction"}})
    target["content"] = {}
    endpoint.respond("GET", f"{ROOM_PATH}/event/$target", target)

    with pytest.raises(ValueError, match="redacted"):
        await events.edit(ROOM, "$target", "updated")


@pytest.mark.parametrize("target_kind", ["replacement", "state"])
async def test_edit_rejects_invalid_replacement_targets_before_write(
    matrix: tuple[MatrixEvents, MatrixEndpoint], target_kind: str
) -> None:
    events, endpoint = matrix
    target = message("$target")
    if target_kind == "replacement":
        target["content"]["m.relates_to"] = {
            "rel_type": "m.replace",
            "event_id": "$original",
        }
    else:
        target["state_key"] = ""
    endpoint.respond("GET", f"{ROOM_PATH}/event/$target", target)

    with pytest.raises(ValueError, match="replacement target"):
        await events.edit(ROOM, "$target", "updated")

    assert len(endpoint.requests) == 1


async def test_new_content_actions_refuse_encrypted_rooms_before_send(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    endpoint.respond(
        "GET",
        f"{ROOM_PATH}/state/m.room.encryption",
        {"algorithm": "m.megolm.v1.aes-sha2"},
    )

    with pytest.raises(RuntimeError, match="encrypted"):
        await events.react(ROOM, "$target", "👍")

    assert len(endpoint.requests) == 1


async def test_rate_limit_retry_keeps_transaction_path_and_payload(
    matrix: tuple[MatrixEvents, MatrixEndpoint],
) -> None:
    events, endpoint = matrix
    path = f"{ROOM_PATH}/send/m.reaction/stable-txn"
    endpoint.respond(
        "PUT",
        path,
        {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 0},
        status=429,
    )
    endpoint.respond("PUT", path, {"event_id": "$reaction"})

    await events.react(ROOM, "$target", "👍", transaction_id="stable-txn")

    writes = [request for request in endpoint.requests if request["method"] == "PUT"]
    assert [request["path"] for request in writes] == [path, path]
    assert writes[0]["body"] == writes[1]["body"]


@pytest.mark.parametrize("transaction_id", ["", ".", ".."])
async def test_event_actions_reject_unsafe_transaction_ids_before_http(
    matrix: tuple[MatrixEvents, MatrixEndpoint], transaction_id: str
) -> None:
    events, endpoint = matrix

    with pytest.raises(ValueError, match="transaction"):
        await events.react(ROOM, "$target", "👍", transaction_id=transaction_id)

    assert endpoint.requests == []


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("history", ("room",)),
        ("context", (ROOM, "event")),
        ("react", (ROOM, "$target", "")),
        ("redact", (ROOM, "$target")),
    ],
)
async def test_invalid_inputs_are_rejected_before_http(
    matrix: tuple[MatrixEvents, MatrixEndpoint], method: str, args: tuple[str, ...]
) -> None:
    events, endpoint = matrix

    if method == "redact":
        with pytest.raises(ValueError, match="transaction"):
            await events.redact(*args, transaction_id="bad transaction")
    else:
        with pytest.raises(ValueError, match=r"Invalid Matrix|Reaction key"):
            await getattr(events, method)(*args)

    assert endpoint.requests == []
