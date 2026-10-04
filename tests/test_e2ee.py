from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from nio import AsyncClient, AsyncClientConfig
from nio.crypto.key_export import encrypt_and_save

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.e2ee import (
    MISSING_ROOM_KEY,
    E2EEUnavailableError,
    MatrixE2EE,
    e2ee_store_path,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

USER = "@alice:example.com"
DEVICE = "ALICEDEVICE"
ROOM = "!room:example.com"


@dataclass
class CryptoEndpoint:
    """Just enough homeserver for one device to publish keys and talk to itself."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    device_keys: dict[str, Any] | None = None
    one_time_keys: int = 0
    upload_status: int = 200
    to_device_batches: list[list[dict[str, Any]]] = field(default_factory=list)
    batch: int = 0

    async def handle(self, request: web.Request) -> web.Response:  # noqa: PLR0911
        body = await request.json() if request.can_read_body else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "body": body,
            }
        )
        path = request.path
        if path == "/_matrix/client/v3/keys/upload":
            if self.upload_status != 200:
                return web.json_response(
                    {"errcode": "M_INVALID_PARAM", "error": "Device keys already exist"},
                    status=self.upload_status,
                )
            assert isinstance(body, dict)
            if "device_keys" in body:
                self.device_keys = body["device_keys"]
            self.one_time_keys += len(body.get("one_time_keys", {}))
            return web.json_response(
                {"one_time_key_counts": {"signed_curve25519": self.one_time_keys}}
            )
        if path == "/_matrix/client/v3/sync":
            self.batch += 1
            events = self.to_device_batches.pop(0) if self.to_device_batches else []
            return web.json_response(
                {
                    "next_batch": f"batch-{self.batch}",
                    "to_device": {"events": events},
                    "device_one_time_keys_count": {"signed_curve25519": self.one_time_keys},
                }
            )
        if path.endswith("/joined_members"):
            return web.json_response({"joined": {USER: {}}})
        if path == "/_matrix/client/v3/keys/query":
            return web.json_response(
                {"device_keys": {USER: {DEVICE: self.device_keys}}, "failures": {}}
            )
        if path == "/_matrix/client/v3/keys/claim":
            return web.json_response({"one_time_keys": {}, "failures": {}})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    def syncs(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["path"].endswith("/sync")]


@pytest.fixture
async def homeserver() -> AsyncIterator[tuple[MatrixMCPConfig, CryptoEndpoint]]:
    endpoint = CryptoEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        config = MatrixMCPConfig(
            homeserver=str(server.make_url("/")),
            user_id=USER,
            device_id=DEVICE,
            access_token="test-token",
        )
        yield config, endpoint


def megolm_event(event_id: str, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": USER,
        "origin_server_ts": 100,
        "type": "m.room.encrypted",
        "content": content,
        "unsigned": {},
    }


def test_store_path_is_device_specific(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("matrix_mcp.e2ee.default_config_path", lambda: tmp_path / "config.json")
    first = MatrixMCPConfig(
        homeserver="https://matrix.example.com",
        user_id=USER,
        device_id="ONE",
        access_token="token",
    )
    second = first.model_copy(update={"device_id": "TWO"})

    assert e2ee_store_path(first).parent == tmp_path
    assert e2ee_store_path(first).name.startswith("e2ee-")
    assert e2ee_store_path(first) != e2ee_store_path(second)


async def test_setup_uploads_keys_into_a_private_store(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    store = tmp_path / "store"

    status = await MatrixE2EE(config, store_path=store).setup()

    assert endpoint.device_keys is not None
    assert status.device_id == DEVICE
    assert status.fingerprint == endpoint.device_keys["keys"][f"ed25519:{DEVICE}"]
    assert status.store_path == store
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert endpoint.one_time_keys > 0


async def test_catch_up_drains_to_device_and_resumes_from_stored_token(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.to_device_batches = [[{"type": "m.dummy", "sender": USER, "content": {}}]]

    await MatrixE2EE(config, store_path=tmp_path).setup()
    first_syncs = endpoint.syncs()
    await MatrixE2EE(config, store_path=tmp_path).setup()

    assert len(first_syncs) == 2
    assert "since" not in first_syncs[0]["query"]
    assert first_syncs[1]["query"]["since"] == "batch-1"
    assert endpoint.syncs()[2]["query"]["since"] == "batch-2"
    assert json.loads(first_syncs[0]["query"]["filter"])["room"] == {"rooms": []}


async def test_encrypted_message_decrypts_in_a_later_session(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    writer = MatrixE2EE(config, store_path=tmp_path)
    content = {
        "msgtype": "m.text",
        "body": "secret",
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
    }
    try:
        event_type, encrypted = await writer.encrypt(ROOM, "m.room.message", content)
    finally:
        await writer.aclose()

    assert event_type == "m.room.encrypted"
    assert "secret" not in str(encrypted)
    assert encrypted["m.relates_to"] == content["m.relates_to"]

    reader = MatrixE2EE(config, store_path=tmp_path)
    try:
        result = await reader.decrypt(ROOM, megolm_event("$secret", encrypted))
    finally:
        await reader.aclose()

    assert result.error is None
    assert result.event["type"] == "m.room.message"
    assert result.event["event_id"] == "$secret"
    assert result.event["content"]["body"] == "secret"
    assert result.event["content"]["m.relates_to"] == content["m.relates_to"]


async def test_bundled_encrypted_replacement_is_decrypted(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    crypto = MatrixE2EE(config, store_path=tmp_path)
    try:
        _, original = await crypto.encrypt(
            ROOM, "m.room.message", {"msgtype": "m.text", "body": "a"}
        )
        _, edit = await crypto.encrypt(
            ROOM,
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": "* b",
                "m.new_content": {"msgtype": "m.text", "body": "b"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        )
        raw = megolm_event("$original", original)
        raw["unsigned"] = {"m.relations": {"m.replace": megolm_event("$edit", edit)}}

        result = await crypto.decrypt(ROOM, raw)
    finally:
        await crypto.aclose()

    replacement = result.event["unsigned"]["m.relations"]["m.replace"]
    assert result.event["content"]["body"] == "a"
    assert replacement["type"] == "m.room.message"
    assert replacement["content"]["m.new_content"]["body"] == "b"


async def test_unknown_session_reports_missing_room_key(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    raw = megolm_event(
        "$unknown",
        {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "AwgAEhA",
            "sender_key": "c2VuZGVyLWtleQ",
            "session_id": "c2Vzc2lvbi1pZA",
            "device_id": "OTHER",
        },
    )
    crypto = MatrixE2EE(config, store_path=tmp_path)
    try:
        result = await crypto.decrypt(ROOM, raw)
    finally:
        await crypto.aclose()

    assert result.error == MISSING_ROOM_KEY
    assert result.event is raw


async def test_malformed_encrypted_event_is_reported(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    crypto = MatrixE2EE(config, store_path=tmp_path)
    try:
        result = await crypto.decrypt(ROOM, megolm_event("$bad", {"algorithm": "unknown"}))
    finally:
        await crypto.aclose()

    assert result.error == "unsupported encrypted event"


async def test_busy_store_times_out_for_a_second_session(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    holder = MatrixE2EE(config, store_path=tmp_path)
    contender = MatrixE2EE(config, store_path=tmp_path, lock_timeout=0.05)
    try:
        await holder.encrypt(ROOM, "m.room.message", {"msgtype": "m.text", "body": "hold"})

        with pytest.raises(E2EEUnavailableError, match="in use"):
            await contender.setup()
        result = await contender.decrypt(ROOM, megolm_event("$any", {}))
        with pytest.raises(RuntimeError, match="in use"):
            await contender.encrypt(ROOM, "m.room.message", {"body": "x"})
    finally:
        await holder.aclose()

    assert result.error is not None
    assert "in use" in result.error


async def test_rejected_key_upload_makes_encryption_unavailable(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.upload_status = 400

    with pytest.raises(E2EEUnavailableError, match="dedicated device"):
        await MatrixE2EE(config, store_path=tmp_path).setup()

    assert endpoint.syncs() == []


async def test_imported_room_keys_decrypt_older_history(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    writer_store = tmp_path / "writer"
    writer = MatrixE2EE(config, store_path=writer_store)
    try:
        _, encrypted = await writer.encrypt(
            ROOM, "m.room.message", {"msgtype": "m.text", "body": "old"}
        )
    finally:
        await writer.aclose()
    export = tmp_path / "keys.txt"
    exporter = AsyncClient(
        config.normalized_homeserver,
        USER,
        device_id=DEVICE,
        store_path=str(writer_store),
        config=AsyncClientConfig(encryption_enabled=True),
    )
    exporter.restore_login(USER, DEVICE, "test-token")
    await exporter.export_keys(str(export), "correct horse", count=1000)
    await exporter.close()
    raw = megolm_event("$old", encrypted)

    reader_store = tmp_path / "reader"
    reader = MatrixE2EE(config, store_path=reader_store)
    try:
        before = await reader.decrypt(ROOM, raw)
    finally:
        await reader.aclose()
    await MatrixE2EE(config, store_path=reader_store).import_keys(export, "correct horse")
    reader = MatrixE2EE(config, store_path=reader_store)
    try:
        after = await reader.decrypt(ROOM, raw)
    finally:
        await reader.aclose()

    assert before.error == MISSING_ROOM_KEY
    assert after.error is None
    assert after.event["content"]["body"] == "old"


@pytest.mark.parametrize("contents", ["valid", "malformed"])
async def test_import_keys_reports_unusable_exports(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path, contents: str
) -> None:
    config, _endpoint = homeserver
    export = tmp_path / "keys.txt"
    if contents == "valid":
        encrypt_and_save(b"[]", str(export), "right", count=1000)
    else:
        export.write_text("not a key export", encoding="utf-8")

    with pytest.raises(ValueError, match="passphrase"):
        await MatrixE2EE(config, store_path=tmp_path / "store").import_keys(export, "wrong")
