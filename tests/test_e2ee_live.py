"""End-to-end encryption against a real homeserver.

Opt in by pointing the test at a disposable homeserver that allows registration
with a token:

    MATRIX_MCP_LIVE_HOMESERVER=http://127.0.0.1:8008 \
    MATRIX_MCP_LIVE_REGISTRATION_TOKEN=... uv run pytest tests/test_e2ee_live.py
"""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx
import pytest
from anyio import Path as AsyncPath
from nio import AsyncClient, AsyncClientConfig, DownloadResponse, SyncResponse, UploadResponse
from nio.crypto.attachments import decrypt_attachment

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.e2ee import MatrixE2EE
from matrix_mcp.matrix_client import MatrixAPIClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

HOMESERVER = os.environ.get("MATRIX_MCP_LIVE_HOMESERVER", "")
REGISTRATION_TOKEN = os.environ.get("MATRIX_MCP_LIVE_REGISTRATION_TOKEN", "")
SECRET_FILE = b"encrypted attachment bytes"

pytestmark = pytest.mark.skipif(
    not HOMESERVER or not REGISTRATION_TOKEN,
    reason="set MATRIX_MCP_LIVE_HOMESERVER and MATRIX_MCP_LIVE_REGISTRATION_TOKEN",
)


async def register(name: str) -> dict[str, str]:
    body: dict[str, Any] = {"username": name, "password": f"password-{name}"}
    async with httpx.AsyncClient(base_url=HOMESERVER) as http:
        challenge = await http.post("/_matrix/client/v3/register", json=body)
        session = challenge.json()["session"]
        for stage in ("m.login.registration_token", "m.login.dummy"):
            auth: dict[str, str] = {"type": stage, "session": session}
            if stage == "m.login.registration_token":
                auth["token"] = REGISTRATION_TOKEN
            response = await http.post("/_matrix/client/v3/register", json={**body, "auth": auth})
            if response.status_code == httpx.codes.OK:
                return dict(response.json())
    msg = f"Could not register {name}: {response.text}"
    raise RuntimeError(msg)


async def other_client(account: dict[str, str], store: Path) -> AsyncClient:
    """A separate E2EE-capable Matrix client, standing in for the user's chat app."""
    await AsyncPath(store).mkdir()
    client = AsyncClient(
        HOMESERVER,
        account["user_id"],
        device_id=account["device_id"],
        store_path=str(store),
        config=AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True),
    )
    client.restore_login(account["user_id"], account["device_id"], account["access_token"])
    await client.keys_upload()
    return client


async def timeline(client: AsyncClient, room_id: str) -> list[Any]:
    response = await client.sync(timeout=0, full_state=True)
    assert isinstance(response, SyncResponse)
    room = response.rooms.join.get(room_id)
    return [] if room is None else list(room.timeline.events)


@pytest.fixture
async def accounts(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, MatrixMCPConfig]]:
    suffix = uuid4().hex[:8]
    alice = await register(f"alice{suffix}")
    bob = await register(f"bob{suffix}")
    chat_app = await other_client(alice, tmp_path / "alice")
    config = MatrixMCPConfig(
        homeserver=HOMESERVER,
        user_id=bob["user_id"],
        device_id=bob["device_id"],
        access_token=bob["access_token"],
    )
    try:
        yield chat_app, config
    finally:
        await chat_app.close()


@asynccontextmanager
async def tool_call(config: MatrixMCPConfig) -> AsyncIterator[MatrixAPIClient]:
    """One MCP tool call: a fresh client and crypto session, closed afterwards."""
    client = MatrixAPIClient(config=config)
    try:
        yield client
    finally:
        await client.aclose()


async def encrypted_room_with_mcp(chat_app: AsyncClient, config: MatrixMCPConfig) -> str:
    created = await chat_app.room_create(
        invite=[config.user_id],
        initial_state=[
            {
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }
        ],
    )
    async with tool_call(config) as client:
        await client.rooms.join(created.room_id)
    # Let the chat app see the new member before it shares a room key.
    await timeline(chat_app, created.room_id)
    return cast("str", created.room_id)


async def send_question_and_file(
    chat_app: AsyncClient, room_id: str, mcp_user_id: str
) -> tuple[str, str]:
    question = await chat_app.room_send(
        room_id,
        "m.room.message",
        {
            "msgtype": "m.text",
            "body": "Encrypted question",
            "m.mentions": {"user_ids": [mcp_user_id]},
        },
        ignore_unverified_devices=True,
    )
    upload, keys = await chat_app.upload(
        lambda *_: SECRET_FILE,
        content_type="application/octet-stream",
        encrypt=True,
        filesize=len(SECRET_FILE),
    )
    assert isinstance(upload, UploadResponse)
    attachment = await chat_app.room_send(
        room_id,
        "m.room.message",
        {
            "msgtype": "m.file",
            "body": "notes.txt",
            "info": {"mimetype": "text/plain", "size": len(SECRET_FILE)},
            "file": {"url": upload.content_uri, **keys},
        },
        ignore_unverified_devices=True,
    )
    return question.event_id, attachment.event_id


async def assert_mcp_reads(
    config: MatrixMCPConfig, room_id: str, question_id: str, attachment_id: str
) -> None:
    async with tool_call(config) as client:
        recent = {event.event_id: event for event in await client.read_room_recent(room_id)}
        page = await client.events.history(room_id)
    assert recent[question_id].body == "Encrypted question"
    assert recent[question_id].encrypted is True
    assert recent[question_id].decryption_error is None
    media = next(event.media for event in page.events if event.event_id == attachment_id)
    assert media is not None
    assert media.encrypted is True

    async with tool_call(config) as client:
        attachment = await client.events.attachment(room_id, attachment_id)
        downloaded = await client.media.download(media.url, attachment=attachment)
    assert base64.b64decode(downloaded.data_base64) == SECRET_FILE
    assert downloaded.content_type == "text/plain"

    async with tool_call(config) as client:
        unread = await client.rooms.unread()
    mentions = [mention.body for room in unread.rooms for mention in room.mentions]
    assert mentions == ["Encrypted question"]


async def assert_chat_app_reads(
    chat_app: AsyncClient, config: MatrixMCPConfig, room_id: str, sent: dict[str, str]
) -> None:
    received = await timeline(chat_app, room_id)
    from_mcp = [event for event in received if event.sender == config.user_id]
    assert from_mcp, "the chat app saw no events from matrix-mcp"
    assert all(getattr(event, "decrypted", False) for event in from_mcp)
    contents = {event.event_id: event.source["content"] for event in from_mcp}
    assert contents[sent["reply"]]["body"] == "Encrypted answer"
    assert contents[sent["reply"]]["m.relates_to"]["event_id"] == sent["question"]
    assert contents[sent["edit"]]["m.new_content"]["body"] == "Encrypted answer, edited"
    assert contents[sent["reaction"]]["m.relates_to"]["key"] == "👍"
    sent_file = contents[sent["file"]]["file"]
    assert "url" not in contents[sent["file"]]
    download = await chat_app.download(mxc=sent_file["url"])
    assert isinstance(download, DownloadResponse)
    plaintext = decrypt_attachment(
        download.body, sent_file["key"]["k"], sent_file["hashes"]["sha256"], sent_file["iv"]
    )
    assert plaintext == b"report contents"


async def test_matrix_mcp_reads_and_writes_an_encrypted_room(
    accounts: tuple[AsyncClient, MatrixMCPConfig], tmp_path: Path
) -> None:
    chat_app, config = accounts
    # Login publishes the matrix-mcp device keys before anyone sends to it.
    status = await MatrixE2EE(config).setup()
    assert status.device_id == config.device_id
    room_id = await encrypted_room_with_mcp(chat_app, config)
    assert config.user_id is not None
    question_id, attachment_id = await send_question_and_file(chat_app, room_id, config.user_id)

    await assert_mcp_reads(config, room_id, question_id, attachment_id)

    report = tmp_path / "report.txt"
    report.write_text("report contents", encoding="utf-8")
    async with tool_call(config) as client:
        reply_id = await client.send_message(room_id, "Encrypted answer", thread_id=question_id)
        sent = {
            "question": question_id,
            "reply": reply_id,
            "reaction": await client.events.react(room_id, question_id, "👍"),
            "edit": await client.events.edit(room_id, reply_id, "Encrypted answer, edited"),
            "file": await client.send_file(room_id, str(report)),
        }
    await assert_chat_app_reads(chat_app, config, room_id, sent)

    async with tool_call(config) as client:
        thread = await client.read_thread(room_id, question_id)
    assert [event.body for event in thread] == ["Encrypted question", "Encrypted answer, edited"]


async def test_invitee_reads_an_encrypted_direct_chat_sent_before_joining(
    accounts: tuple[AsyncClient, MatrixMCPConfig],
) -> None:
    chat_app, config = accounts
    async with tool_call(config) as client:
        direct = await client.rooms.create_dm(chat_app.user_id, encrypted=True)
        await client.send_message(direct.room_id, "Sent before you joined")

    await chat_app.join(direct.room_id)
    events = await timeline(chat_app, direct.room_id)

    assert "Sent before you joined" in [getattr(event, "body", None) for event in events]
