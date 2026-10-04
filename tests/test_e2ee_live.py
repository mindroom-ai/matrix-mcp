"""End-to-end encryption against a real homeserver.

Opt in by pointing the test at a disposable homeserver that allows registration
with a token:

    MATRIX_MCP_LIVE_HOMESERVER=http://127.0.0.1:8008 \
    MATRIX_MCP_LIVE_REGISTRATION_TOKEN=... uv run pytest tests/test_e2ee_live.py
"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
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
    store.mkdir()
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


async def test_matrix_mcp_reads_and_writes_an_encrypted_room(
    accounts: tuple[AsyncClient, MatrixMCPConfig], tmp_path: Path
) -> None:
    chat_app, config = accounts
    # Login publishes the matrix-mcp device keys before anyone sends to it.
    status = await MatrixE2EE(config).setup()
    assert status.device_id == config.device_id

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
    room_id = created.room_id

    async def tool_call() -> MatrixAPIClient:
        return MatrixAPIClient(config=config)

    client = await tool_call()
    try:
        await client.rooms.join(room_id)
    finally:
        await client.aclose()

    await timeline(chat_app, room_id)
    question = await chat_app.room_send(
        room_id,
        "m.room.message",
        {
            "msgtype": "m.text",
            "body": "Encrypted question",
            "m.mentions": {"user_ids": [config.user_id]},
        },
        ignore_unverified_devices=True,
    )
    question_id = question.event_id
    secret_file = b"encrypted attachment bytes"
    upload, keys = await chat_app.upload(
        lambda *_: secret_file,
        content_type="application/octet-stream",
        encrypt=True,
        filesize=len(secret_file),
    )
    assert isinstance(upload, UploadResponse)
    attachment = await chat_app.room_send(
        room_id,
        "m.room.message",
        {
            "msgtype": "m.file",
            "body": "notes.txt",
            "info": {"mimetype": "text/plain", "size": len(secret_file)},
            "file": {"url": upload.content_uri, **keys},
        },
        ignore_unverified_devices=True,
    )

    # Each block below is one MCP tool call with a fresh client and crypto session.
    client = await tool_call()
    try:
        recent = await client.read_room_recent(room_id, limit=10)
        page = await client.events.history(room_id, limit=10)
    finally:
        await client.aclose()
    by_id = {event.event_id: event for event in recent}
    assert by_id[question_id].body == "Encrypted question"
    assert by_id[question_id].encrypted is True
    assert by_id[question_id].decryption_error is None
    media = next(event.media for event in page.events if event.event_id == attachment.event_id)
    assert media is not None
    assert media.encrypted is True

    client = await tool_call()
    try:
        downloaded = await client.media.download(
            media.url,
            attachment=await client.events.attachment(room_id, attachment.event_id),
        )
    finally:
        await client.aclose()
    assert base64.b64decode(downloaded.data_base64) == secret_file
    assert downloaded.content_type == "text/plain"

    client = await tool_call()
    try:
        unread = await client.rooms.unread()
    finally:
        await client.aclose()
    mentions = [mention for room in unread.rooms for mention in room.mentions]
    assert [mention.body for mention in mentions] == ["Encrypted question"]

    client = await tool_call()
    try:
        reply_id = await client.send_message(room_id, "Encrypted answer", thread_id=question_id)
        await client.events.react(room_id, question_id, "👍")
        await client.events.edit(room_id, reply_id, "Encrypted answer, edited")
        report = tmp_path / "report.txt"
        report.write_text("report contents", encoding="utf-8")
        file_id = await client.send_file(room_id, str(report))
    finally:
        await client.aclose()

    received = await timeline(chat_app, room_id)
    sent_by_mcp = [event for event in received if event.sender == config.user_id]
    assert sent_by_mcp, "the chat app saw no events from matrix-mcp"
    assert all(getattr(event, "decrypted", False) for event in sent_by_mcp)
    contents = {event.event_id: event.source["content"] for event in sent_by_mcp}
    assert contents[reply_id]["body"] == "Encrypted answer"
    assert contents[reply_id]["m.relates_to"]["event_id"] == question_id
    edits = [content for content in contents.values() if "m.new_content" in content]
    assert edits[0]["m.new_content"]["body"] == "Encrypted answer, edited"
    reactions = [
        content
        for content in contents.values()
        if content.get("m.relates_to", {}).get("rel_type") == "m.annotation"
    ]
    assert reactions[0]["m.relates_to"]["key"] == "👍"
    sent_file = contents[file_id]["file"]
    assert "url" not in contents[file_id]
    download = await chat_app.download(mxc=sent_file["url"])
    assert isinstance(download, DownloadResponse)
    plaintext = decrypt_attachment(
        download.body, sent_file["key"]["k"], sent_file["hashes"]["sha256"], sent_file["iv"]
    )
    assert plaintext == b"report contents"

    client = await tool_call()
    try:
        thread = await client.read_thread(room_id, question_id)
    finally:
        await client.aclose()
    assert [event.body for event in thread] == [
        "Encrypted question",
        "Encrypted answer, edited",
    ]
