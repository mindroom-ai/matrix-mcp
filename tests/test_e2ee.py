from __future__ import annotations

import base64
import json
import logging
import stat
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from anyio import Path as AsyncPath
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nio import AsyncClient, AsyncClientConfig
from nio.crypto import Olm
from nio.crypto.key_export import decrypt_and_read, encrypt_and_save
from peewee import OperationalError

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.cross_signing import SecretStorageError
from matrix_mcp.e2ee import (
    MISSING_ROOM_KEY,
    E2EEUnavailableError,
    MatrixE2EE,
    e2ee_lock_path,
    e2ee_store_path,
    undelivered_devices,
)
from matrix_mcp.sas_verification import SasVerification, VerificationCancelledError
from tests.sas_helpers import InitiatingDevice, signed_device_keys
from tests.secret_storage_helpers import (
    encrypt_secret,
    key_description,
    pbkdf2_key,
    recovery_key,
    unpadded,
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
    whoami_device: str = DEVICE
    other_devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Synapse lists joined rooms in the key-only catch-up sync, without their state.
    sync_lists_room: bool = False
    invited: list[str] = field(default_factory=list)
    history_visibility: str | None = None
    members_status: int = 200
    account_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    self_signing_keys: dict[str, Any] | None = None
    signature_uploads: list[dict[str, Any]] = field(default_factory=list)
    long_poll_batches: list[list[dict[str, Any]]] = field(default_factory=list)
    sent_to_device: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    other_device: Any = None
    key_queries: int = 0
    failing_key_queries_from: int | None = None

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
        if path == "/_matrix/client/v3/account/whoami":
            return web.json_response({"user_id": USER, "device_id": self.whoami_device})
        if path == "/_matrix/client/v3/keys/upload":
            assert isinstance(body, dict)
            return self.upload(body)
        if path == "/_matrix/client/v3/sync":
            self.batch += 1
            queue = self.long_poll_batches if "timeout" in request.query else self.to_device_batches
            events = queue.pop(0) if queue else []
            rooms: dict[str, Any] = {"join": {ROOM: {}}} if self.sync_lists_room else {}
            return web.json_response(
                {
                    "next_batch": f"batch-{self.batch}",
                    "to_device": {"events": events},
                    "device_one_time_keys_count": {"signed_curve25519": self.one_time_keys},
                    "rooms": rooms,
                }
            )
        if path.endswith("/joined_members"):
            return web.json_response({"joined": {USER: {}}})
        if path.endswith("/members"):
            assert request.query["membership"] == "invite"
            if self.members_status != 200:
                return web.json_response({"errcode": "M_UNKNOWN"}, status=self.members_status)
            chunk = [
                {
                    "type": "m.room.member",
                    "state_key": user_id,
                    "content": {"membership": "invite"},
                }
                for user_id in self.invited
            ]
            return web.json_response({"chunk": chunk})
        if path.endswith("/state/m.room.history_visibility") and self.history_visibility:
            return web.json_response({"history_visibility": self.history_visibility})
        if path == "/_matrix/client/v3/keys/claim":
            return web.json_response({"one_time_keys": {}, "failures": {}})
        return self.cross_signing(path, body)

    def cross_signing(self, path: str, body: Any) -> web.Response:
        if path == "/_matrix/client/v3/keys/query":
            self.key_queries += 1
            devices = {DEVICE: self.device_keys} if self.device_keys else {}
            devices.update(self.other_devices)
            failing = self.failing_key_queries_from is not None and (
                self.key_queries >= self.failing_key_queries_from
            )
            failures = {"example.com": {"errcode": "M_UNKNOWN"}} if failing else {}
            response: dict[str, Any] = {"device_keys": {USER: devices}, "failures": failures}
            if self.self_signing_keys is not None:
                response["self_signing_keys"] = {USER: self.self_signing_keys}
            return web.json_response(response)
        if path == "/_matrix/client/v3/keys/signatures/upload":
            assert isinstance(body, dict)
            self.signature_uploads.append(body)
            return web.json_response({"failures": {}})
        if path.startswith("/_matrix/client/v3/sendToDevice/"):
            assert isinstance(body, dict)
            event_type = path.split("/")[5]
            for devices in body["messages"].values():
                for content in devices.values():
                    self.sent_to_device.append((event_type, content))
                    if self.other_device is not None:
                        self.long_poll_batches.append(self.other_device.answer(event_type, content))
            return web.json_response({})
        stored = None
        if path.startswith(f"/_matrix/client/v3/user/{USER}/account_data/"):
            stored = self.account_data.get(path.rsplit("/", 1)[1])
        if stored is None:
            return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)
        return web.json_response(stored)

    def upload(self, body: dict[str, Any]) -> web.Response:
        if self.upload_status != 200:
            return web.json_response(
                {"errcode": "M_INVALID_PARAM", "error": "Device keys already exist"},
                status=self.upload_status,
            )
        if "device_keys" in body:
            if body["device_keys"]["device_id"] == DEVICE:
                self.device_keys = body["device_keys"]
            else:
                self.other_devices[body["device_keys"]["device_id"]] = body["device_keys"]
        self.one_time_keys += len(body.get("one_time_keys", {}))
        return web.json_response({"one_time_key_counts": {"signed_curve25519": self.one_time_keys}})

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
    # A repeated identical sync could be answered from Synapse's cache, hiding new room keys.
    filters = {sync["query"]["filter"] for sync in endpoint.syncs()}
    assert len(filters) == len(endpoint.syncs())
    # Homeservers answer full_state syncs without a minimum long-poll.
    assert all(sync["query"]["full_state"] == "true" for sync in endpoint.syncs())


async def test_encrypts_for_a_room_the_catch_up_sync_listed_without_state(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.sync_lists_room = True
    writer = MatrixE2EE(config, store_path=tmp_path)
    try:
        event_type, encrypted = await writer.encrypt(ROOM, "m.room.message", {"body": "secret"})
    finally:
        await writer.aclose()

    assert event_type == "m.room.encrypted"
    assert "secret" not in str(encrypted)


def queried_users(endpoint: CryptoEndpoint) -> set[str]:
    queries = [r["body"] for r in endpoint.requests if r["path"].endswith("/keys/query")]
    return {user for query in queries for user in query["device_keys"]}


@pytest.mark.parametrize(
    ("visibility", "shared"),
    [(None, True), ("shared", True), ("invited", True), ("joined", False)],
)
async def test_room_keys_reach_invited_members_unless_history_is_joined_only(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    visibility: str | None,
    *,
    shared: bool,
) -> None:
    config, endpoint = homeserver
    endpoint.invited = ["@bob:example.com"]
    endpoint.history_visibility = visibility
    writer = MatrixE2EE(config, store_path=tmp_path)
    try:
        await writer.encrypt(ROOM, "m.room.message", {"body": "before bob joins"})
        assert writer._client is not None  # noqa: SLF001
        room = writer._client.rooms[ROOM]  # noqa: SLF001
        assert ("@bob:example.com" in room.users) is shared
        assert ("@bob:example.com" in queried_users(endpoint)) is shared
        # A withdrawn invitation stops further room keys from going to that user.
        endpoint.invited = []
        await writer.encrypt(ROOM, "m.room.message", {"body": "after the invite is withdrawn"})
        assert "@bob:example.com" not in room.users
    finally:
        await writer.aclose()


async def test_failed_invitee_lookup_refuses_to_encrypt(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.invited = ["@bob:example.com"]
    endpoint.members_status = 500
    writer = MatrixE2EE(config, store_path=tmp_path)
    try:
        # Bob could never read a message whose key skipped him, so it must not go out.
        with pytest.raises(RuntimeError, match="invited members; the message was not sent"):
            await writer.encrypt(ROOM, "m.room.message", {"body": "for bob"})
    finally:
        await writer.aclose()


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

    with pytest.raises(E2EEUnavailableError, match="upload failed"):
        await MatrixE2EE(config, store_path=tmp_path).setup()

    assert endpoint.syncs() == []


async def test_imported_room_keys_decrypt_older_history(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
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

    # The export is imported on a different device of the same account.
    reader_config = config.model_copy(update={"device_id": "READERDEVICE"})
    endpoint.whoami_device = "READERDEVICE"
    reader_store = tmp_path / "reader"
    reader = MatrixE2EE(reader_config, store_path=reader_store)
    try:
        before = await reader.decrypt(ROOM, raw)
    finally:
        await reader.aclose()
    await MatrixE2EE(reader_config, store_path=reader_store).import_keys(export, "correct horse")
    reader = MatrixE2EE(reader_config, store_path=reader_store)
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


async def test_keys_another_client_published_for_the_device_are_never_replaced(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.device_keys = {
        "user_id": USER,
        "device_id": DEVICE,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {f"curve25519:{DEVICE}": "Y3VydmU", f"ed25519:{DEVICE}": "ZWQyNTUxOQ"},
        "signatures": {USER: {f"ed25519:{DEVICE}": "c2lnbmF0dXJl"}},
    }

    with pytest.raises(E2EEUnavailableError, match="another Matrix client"):
        await MatrixE2EE(config, store_path=tmp_path).setup()

    assert not any(request["path"].endswith("/keys/upload") for request in endpoint.requests)


async def test_reopened_store_keeps_its_published_keys(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    first = await MatrixE2EE(config, store_path=tmp_path).setup()

    second = await MatrixE2EE(config, store_path=tmp_path).setup()

    assert second.fingerprint == first.fingerprint
    assert [request["body"].get("device_keys") is not None for request in uploads(endpoint)] == [
        True
    ]


@pytest.mark.parametrize("change", ["token device", "published keys", "missing keys"])
async def test_reopened_store_checks_the_device_identity_every_time(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path, change: str
) -> None:
    config, endpoint = homeserver
    await MatrixE2EE(config, store_path=tmp_path).setup()
    syncs_before = len(endpoint.syncs())
    assert endpoint.device_keys is not None
    if change == "token device":
        endpoint.whoami_device = "OTHERDEVICE"
    elif change == "published keys":
        endpoint.device_keys["keys"][f"ed25519:{DEVICE}"] = "c29tZW9uZSBlbHNl"
    else:
        endpoint.device_keys = None

    with pytest.raises(E2EEUnavailableError, match="device") as caught:
        await MatrixE2EE(config, store_path=tmp_path).setup()

    if change == "published keys":
        assert "auth logout" in str(caught.value)
    assert len(endpoint.syncs()) == syncs_before
    assert await AsyncPath(tmp_path).exists()


async def export_room_keys(
    config: MatrixMCPConfig, store: Path, export: Path, *, room_id: str | None = None
) -> None:
    """Export a store's room keys, optionally relabeling them for another room."""
    exporter = AsyncClient(
        config.normalized_homeserver,
        USER,
        device_id=DEVICE,
        store_path=str(store),
        config=AsyncClientConfig(encryption_enabled=True),
    )
    exporter.restore_login(USER, DEVICE, "test-token")
    await exporter.export_keys(str(export), "pass", count=1000)
    await exporter.close()
    if room_id is not None:
        sessions = json.loads(decrypt_and_read(str(export), "pass"))
        for session in sessions:
            session["room_id"] = room_id
        await AsyncPath(export).unlink()
        encrypt_and_save(json.dumps(sessions).encode(), str(export), "pass", count=1000)


async def test_decrypted_payload_must_belong_to_the_requested_room(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    writer = MatrixE2EE(config, store_path=tmp_path / "writer")
    try:
        _, encrypted = await writer.encrypt(
            ROOM, "m.room.message", {"msgtype": "m.text", "body": "x"}
        )
    finally:
        await writer.aclose()
    export = tmp_path / "keys.txt"
    other_room = "!other:example.com"
    await export_room_keys(config, tmp_path / "writer", export, room_id=other_room)
    reader_config = config.model_copy(update={"device_id": "READERDEVICE"})
    endpoint.whoami_device = "READERDEVICE"
    await MatrixE2EE(reader_config, store_path=tmp_path / "reader").import_keys(export, "pass")

    reader = MatrixE2EE(reader_config, store_path=tmp_path / "reader")
    try:
        result = await reader.decrypt(other_room, megolm_event("$moved", encrypted))
    finally:
        await reader.aclose()

    assert result.error == "decrypted event belongs to a different room"
    assert result.event["type"] == "m.room.encrypted"


async def test_plaintext_bundle_cannot_edit_an_encrypted_message(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    crypto = MatrixE2EE(config, store_path=tmp_path)
    try:
        _, original = await crypto.encrypt(
            ROOM, "m.room.message", {"msgtype": "m.text", "body": "a"}
        )
        raw = megolm_event("$original", original)
        raw["unsigned"] = {
            "m.relations": {
                "m.replace": {
                    "event_id": "$forged",
                    "sender": USER,
                    "origin_server_ts": 200,
                    "type": "m.room.message",
                    "content": {
                        "msgtype": "m.text",
                        "body": "* forged",
                        "m.new_content": {"msgtype": "m.text", "body": "forged"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                    },
                }
            }
        }

        result = await crypto.decrypt(ROOM, raw)
    finally:
        await crypto.aclose()

    assert result.event["content"]["body"] == "a"
    assert result.event["unsigned"]["m.relations"]["m.replace"] == {"event_id": "$forged"}


async def test_sync_token_advances_only_after_room_keys_are_processed(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, endpoint = homeserver
    await MatrixE2EE(config, store_path=tmp_path).setup()

    async def crash(_client: object, _response: object) -> None:
        msg = "interrupted while storing room keys"
        raise RuntimeError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncClient, "_handle_to_device", crash)
        with pytest.raises(E2EEUnavailableError, match="setup failed"):
            await MatrixE2EE(config, store_path=tmp_path).setup()
    await MatrixE2EE(config, store_path=tmp_path).setup()

    since = [sync["query"].get("since") for sync in endpoint.syncs()]
    assert since == [None, "batch-1", "batch-1"]


async def test_store_lock_sits_beside_the_store(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    store = tmp_path / "store"

    await MatrixE2EE(config, store_path=store).setup()

    assert e2ee_lock_path(store) == tmp_path / "store.lock"
    assert not (store / "store.lock").exists()


def test_undelivered_devices_lists_reachable_devices_missing_the_room_key() -> None:
    def device(device_id: str, curve: str) -> SimpleNamespace:
        return SimpleNamespace(id=device_id, curve25519=curve)

    devices = {
        "@alice:example.com": [device(DEVICE, "own"), device("PHONE", "phone")],
        "@bob:example.com": [device("LAPTOP", "laptop"), device("OLD", "no-session")],
    }
    olm = SimpleNamespace(
        device_id=DEVICE,
        device_store=SimpleNamespace(active_user_devices=lambda user: devices[user]),
        session_store=SimpleNamespace(get=lambda curve: None if curve == "no-session" else curve),
        is_device_blacklisted=lambda _device: False,
    )

    missing = undelivered_devices(
        cast("Olm", olm),
        ["@alice:example.com", "@bob:example.com"],
        {("@alice:example.com", "PHONE")},
    )

    assert missing == {("@bob:example.com", "LAPTOP")}


def uploads(endpoint: CryptoEndpoint) -> list[dict[str, Any]]:
    return [request for request in endpoint.requests if request["path"].endswith("/keys/upload")]


async def test_first_open_refuses_credentials_for_a_different_device(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.whoami_device = "TOKENDEVICE"

    with pytest.raises(E2EEUnavailableError, match="device"):
        await MatrixE2EE(config, store_path=tmp_path).setup()

    assert uploads(endpoint) == []


async def test_drained_one_time_keys_are_replenished(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    await MatrixE2EE(config, store_path=tmp_path).setup()
    endpoint.one_time_keys = 0

    await MatrixE2EE(config, store_path=tmp_path).setup()

    replenished = uploads(endpoint)[-1]["body"]
    assert "device_keys" not in replenished
    assert replenished["one_time_keys"]


async def test_malformed_to_device_message_is_skipped_without_losing_progress(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    await MatrixE2EE(config, store_path=tmp_path).setup()
    assert endpoint.device_keys is not None
    our_key = endpoint.device_keys["keys"][f"curve25519:{DEVICE}"]
    endpoint.to_device_batches = [
        [
            {
                "type": "m.room.encrypted",
                "sender": "@mallory:example.com",
                "content": {
                    "algorithm": "m.olm.v1.curve25519-aes-sha2",
                    "sender_key": "x",
                    "ciphertext": {our_key: {"type": 0, "body": "!!!"}},
                },
            }
        ]
    ]

    await MatrixE2EE(config, store_path=tmp_path).setup()
    await MatrixE2EE(config, store_path=tmp_path).setup()

    since = [sync["query"].get("since") for sync in endpoint.syncs()]
    assert since == [None, "batch-1", "batch-2", "batch-3"]


async def test_schema_invalid_payload_is_read_without_logging_its_content(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config, _endpoint = homeserver
    caplog.set_level(logging.DEBUG)
    crypto = MatrixE2EE(config, store_path=tmp_path)
    try:
        _, encrypted = await crypto.encrypt(
            ROOM, "m.room.message", {"msgtype": "m.text", "note": "classified"}
        )
        result = await crypto.decrypt(ROOM, megolm_event("$odd", encrypted))
    finally:
        await crypto.aclose()

    assert result.error is None
    assert result.event["content"] == {"msgtype": "m.text", "note": "classified"}
    assert "classified" not in caplog.text
    assert "classified" not in repr(result)


async def test_queued_to_device_messages_are_sent_after_catch_up(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _endpoint = homeserver
    sent: list[bool] = []

    async def send_to_device_messages(_client: object) -> list[object]:
        sent.append(True)
        return []

    monkeypatch.setattr(AsyncClient, "send_to_device_messages", send_to_device_messages)

    await MatrixE2EE(config, store_path=tmp_path).setup()

    assert sent == [True]


async def test_local_storage_errors_stop_the_catch_up_without_acknowledging_keys(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, endpoint = homeserver
    await MatrixE2EE(config, store_path=tmp_path).setup()
    endpoint.to_device_batches = [[{"type": "m.dummy", "sender": USER, "content": {}}]]

    def disk_full(_olm: object, _event: object) -> None:
        msg = "database or disk is full"
        raise OperationalError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(Olm, "handle_to_device_event", disk_full)
        with pytest.raises(E2EEUnavailableError, match="OperationalError"):
            await MatrixE2EE(config, store_path=tmp_path).setup()
    await MatrixE2EE(config, store_path=tmp_path).setup()

    since = [sync["query"].get("since") for sync in endpoint.syncs()]
    # The failed batch was not acknowledged, so the next call fetches it again.
    assert since == [None, "batch-1", "batch-1"]


def test_only_loggers_that_embed_event_content_are_silenced() -> None:
    assert logging.getLogger("nio.events.misc").getEffectiveLevel() == logging.CRITICAL
    assert logging.getLogger("nio.crypto.log").getEffectiveLevel() == logging.CRITICAL
    assert logging.getLogger("nio.http").getEffectiveLevel() < logging.CRITICAL
    assert logging.getLogger("nio.responses").getEffectiveLevel() < logging.CRITICAL


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        {"type": "m.room.message", "content": {"msgtype": "m.text"}, "unsigned": "x"},
    ],
)
async def test_hostile_payload_stays_encrypted_instead_of_failing_the_read(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path, payload: object
) -> None:
    config, _endpoint = homeserver
    crypto = MatrixE2EE(config, store_path=tmp_path)
    try:
        await crypto.encrypt(ROOM, "m.room.message", {"msgtype": "m.text", "body": "open"})
        client = await crypto._session()  # noqa: SLF001 - Encrypt an arbitrary plaintext.
        olm = client.olm
        assert olm is not None
        session = olm.outbound_group_sessions[ROOM]
        ciphertext = session.encrypt(json.dumps(payload))
        raw = megolm_event(
            "$hostile",
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": ciphertext,
                "sender_key": olm.account.identity_keys["curve25519"],
                "session_id": session.id,
                "device_id": DEVICE,
            },
        )
        bad_unsigned = {**raw, "unsigned": "x"}

        results = [await crypto.decrypt(ROOM, raw), await crypto.decrypt(ROOM, bad_unsigned)]
    finally:
        await crypto.aclose()

    assert all(result.error is not None for result in results)


async def test_unusable_lock_file_degrades_reads(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _endpoint = homeserver

    async def denied(_lock: object) -> None:
        msg = "read-only file system"
        raise PermissionError(msg)

    monkeypatch.setattr("matrix_mcp.e2ee.AsyncFileLock.acquire", denied)

    result = await MatrixE2EE(config, store_path=tmp_path).decrypt(ROOM, megolm_event("$x", {}))

    assert result.error is not None
    assert "read-only" in result.error


STORAGE_KEY = bytes(range(32))
STORAGE_KEY_ID = "storagekey"
SELF_SIGNING_SEED = bytes(range(64, 96))


def self_signing_id() -> str:
    raw = Ed25519PrivateKey.from_private_bytes(SELF_SIGNING_SEED).public_key().public_bytes_raw()
    return unpadded(raw)


def self_sign(device: dict[str, Any]) -> str:
    signed_part = {
        key: value for key, value in device.items() if key not in ("signatures", "unsigned")
    }
    message = json.dumps(signed_part, separators=(",", ":"), sort_keys=True).encode()
    return unpadded(Ed25519PrivateKey.from_private_bytes(SELF_SIGNING_SEED).sign(message))


def with_secret_storage(
    endpoint: CryptoEndpoint, passphrase: dict[str, Any] | None = None, key: bytes = STORAGE_KEY
) -> None:
    secret = unpadded(SELF_SIGNING_SEED).encode()
    endpoint.account_data = {
        "m.secret_storage.default_key": {"key": STORAGE_KEY_ID},
        f"m.secret_storage.key.{STORAGE_KEY_ID}": key_description(key, passphrase),
        "m.cross_signing.self_signing": {
            "encrypted": {
                STORAGE_KEY_ID: encrypt_secret(
                    key, "m.cross_signing.self_signing", secret, bytes(16)
                )
            }
        },
    }
    endpoint.self_signing_keys = {
        "user_id": USER,
        "usage": ["self_signing"],
        "keys": {f"ed25519:{self_signing_id()}": self_signing_id()},
    }


async def test_verify_signs_this_device_with_the_self_signing_key(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    with_secret_storage(endpoint)

    result = await MatrixE2EE(config, store_path=tmp_path / "store").verify_device(
        recovery_key=recovery_key(STORAGE_KEY)
    )

    assert (result.device_id, result.self_signing_key, result.already_signed) == (
        DEVICE,
        self_signing_id(),
        False,
    )
    [upload] = endpoint.signature_uploads
    signed = upload[USER][DEVICE]
    assert endpoint.device_keys is not None
    assert signed["keys"] == endpoint.device_keys["keys"]
    signature = signed["signatures"][USER][f"ed25519:{self_signing_id()}"]
    unsigned_part = {key: value for key, value in signed.items() if key != "signatures"}
    Ed25519PrivateKey.from_private_bytes(SELF_SIGNING_SEED).public_key().verify(
        base64.b64decode(signature + "=="),
        json.dumps(unsigned_part, separators=(",", ":"), sort_keys=True).encode(),
    )
    assert signed["signatures"][USER][f"ed25519:{DEVICE}"]


async def test_verify_accepts_the_secret_storage_passphrase(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    params = {"algorithm": "m.pbkdf2", "salt": "salt", "iterations": 1000}
    with_secret_storage(endpoint, params, pbkdf2_key("correct horse", "salt", 1000))

    result = await MatrixE2EE(config, store_path=tmp_path / "store").verify_device(
        passphrase="correct horse"
    )

    assert not result.already_signed
    assert len(endpoint.signature_uploads) == 1


async def test_verify_skips_an_already_signed_device(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    with_secret_storage(endpoint)
    e2ee = MatrixE2EE(config, store_path=tmp_path / "store")
    await e2ee.verify_device(recovery_key=recovery_key(STORAGE_KEY))
    assert endpoint.device_keys is not None
    endpoint.device_keys = endpoint.signature_uploads[0][USER][DEVICE]

    result = await e2ee.verify_device(recovery_key=recovery_key(STORAGE_KEY))

    assert result.already_signed
    assert len(endpoint.signature_uploads) == 1


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda endpoint: endpoint.account_data.clear(), "no secret storage"),
        (
            lambda endpoint: endpoint.account_data.pop("m.cross_signing.self_signing"),
            "no m.cross_signing.self_signing secret",
        ),
        (
            lambda endpoint: endpoint.self_signing_keys.update(keys={"ed25519:other": "other"}),
            "not the account's published one",
        ),
        (
            lambda endpoint: endpoint.self_signing_keys.update(
                keys={f"ed25519:{self_signing_id()}": unpadded(bytes(32))}
            ),
            "not the account's published one",
        ),
    ],
)
async def test_verify_refuses_without_a_usable_self_signing_key(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    change: Any,
    error: str,
) -> None:
    config, endpoint = homeserver
    with_secret_storage(endpoint)
    change(endpoint)

    with pytest.raises(SecretStorageError, match=error):
        await MatrixE2EE(config, store_path=tmp_path / "store").verify_device(
            recovery_key=recovery_key(STORAGE_KEY)
        )

    assert endpoint.signature_uploads == []


async def test_verify_replaces_an_invalid_existing_signature(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    with_secret_storage(endpoint)
    e2ee = MatrixE2EE(config, store_path=tmp_path / "store")
    await e2ee.setup()
    assert endpoint.device_keys is not None
    endpoint.device_keys["signatures"][USER][f"ed25519:{self_signing_id()}"] = unpadded(bytes(64))

    result = await e2ee.verify_device(recovery_key=recovery_key(STORAGE_KEY))

    assert not result.already_signed
    assert len(endpoint.signature_uploads) == 1


async def test_verify_refuses_a_device_listed_with_another_curve25519_key(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, endpoint = homeserver
    with_secret_storage(endpoint)
    published_device = MatrixE2EE._published_device  # noqa: SLF001

    async def substituted(e2ee: MatrixE2EE, self_signing: str) -> dict[str, Any]:
        device = await published_device(e2ee, self_signing)
        return {**device, "keys": {**device["keys"], f"curve25519:{DEVICE}": unpadded(bytes(32))}}

    monkeypatch.setattr(MatrixE2EE, "_published_device", substituted)

    with pytest.raises(RuntimeError, match="other encryption keys"):
        await MatrixE2EE(config, store_path=tmp_path / "store").verify_device(
            recovery_key=recovery_key(STORAGE_KEY)
        )

    assert endpoint.signature_uploads == []


async def test_verify_refuses_a_wrong_recovery_key_and_releases_the_store(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    with_secret_storage(endpoint)
    store = tmp_path / "store"

    with pytest.raises(SecretStorageError, match="Wrong recovery key"):
        await MatrixE2EE(config, store_path=store).verify_device(
            recovery_key=recovery_key(bytes(32))
        )

    assert endpoint.signature_uploads == []
    await MatrixE2EE(config, store_path=store, lock_timeout=0.1).setup()


async def test_verify_needs_exactly_one_credential(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver
    e2ee = MatrixE2EE(config, store_path=tmp_path / "store")
    with pytest.raises(ValueError, match="either"):
        await e2ee.verify_device()
    with pytest.raises(ValueError, match="either"):
        await e2ee.verify_device(recovery_key="key", passphrase="phrase")


class ScriptedElement:
    """Another verified client of the user that verifies this device with emoji."""

    def __init__(self, endpoint: CryptoEndpoint) -> None:
        self.endpoint = endpoint
        self.device = InitiatingDevice(USER, "ELEMENT")
        self.accept: dict[str, Any] = {}
        self.signature: str | None = None
        endpoint.other_devices["ELEMENT"] = signed_device_keys(USER, "ELEMENT", self.device.account)
        endpoint.long_poll_batches = [[self.event("request", self.device.request())]]

    def event(self, name: str, content: dict[str, Any]) -> dict[str, Any]:
        return {"sender": USER, "type": f"m.key.verification.{name}", "content": content}

    def answer(self, event_type: str, content: dict[str, Any]) -> list[dict[str, Any]]:
        assert self.endpoint.device_keys is not None
        if event_type == "m.key.verification.ready":
            return [self.event("start", self.device.start())]
        if event_type == "m.key.verification.accept":
            self.accept = content
            return [self.event("key", self.device.key())]
        if event_type == "m.key.verification.key":
            self.device.receive_key(self.accept, content, DEVICE)
            return [self.event("mac", self.device.mac(DEVICE))]
        if event_type == "m.key.verification.mac":
            our_key = self.endpoint.device_keys["keys"][f"ed25519:{DEVICE}"]
            self.device.check_mac(content, DEVICE, our_key)
            return [self.event("done", {"transaction_id": self.device.transaction_id})]
        if event_type == "m.key.verification.done":
            self.endpoint.self_signing_keys = {
                "keys": {f"ed25519:{self_signing_id()}": self_signing_id()}
            }
            self.endpoint.device_keys["signatures"][USER][f"ed25519:{self_signing_id()}"] = (
                self.signature or self_sign(self.endpoint.device_keys)
            )
        return []


async def test_emoji_verification_with_another_device_gets_this_device_signed(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.other_device = ScriptedElement(endpoint)
    shown: list[list[tuple[str, str]]] = []

    async def confirm(verification: SasVerification) -> bool:
        shown.append(verification.emoji)
        return True

    result = await MatrixE2EE(config, store_path=tmp_path / "store").verify_with_device(
        confirm, lambda _message: None, max_wait=5
    )

    assert (result.verified_by, result.signed) == ("ELEMENT", True)
    assert len(shown) == 1
    assert len(shown[0]) == 7
    assert [event_type for event_type, _content in endpoint.sent_to_device] == [
        "m.key.verification.ready",
        "m.key.verification.accept",
        "m.key.verification.key",
        "m.key.verification.mac",
        "m.key.verification.done",
    ]


async def test_rejected_emoji_cancel_the_verification(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.other_device = ScriptedElement(endpoint)

    async def reject(_verification: SasVerification) -> bool:
        return False

    with pytest.raises(VerificationCancelledError, match="did not match"):
        await MatrixE2EE(config, store_path=tmp_path / "store").verify_with_device(
            reject, lambda _message: None, max_wait=5
        )

    event_type, content = endpoint.sent_to_device[-1]
    assert event_type == "m.key.verification.cancel"
    assert content["code"] == "m.mismatched_sas"
    assert "m.key.verification.mac" not in [sent for sent, _content in endpoint.sent_to_device]


async def test_emoji_verification_gives_up_without_a_request(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, _endpoint = homeserver

    async def confirm(_verification: SasVerification) -> bool:
        return True

    with pytest.raises(RuntimeError, match="in time"):
        await MatrixE2EE(config, store_path=tmp_path / "store").verify_with_device(
            confirm, lambda _message: None, max_wait=0.5
        )


async def always_confirm(_verification: SasVerification) -> bool:
    return True


async def test_emoji_verification_rejects_an_invalid_signature(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("matrix_mcp.e2ee._SIGNATURE_CHECK_INTERVAL_SECONDS", 0)
    config, endpoint = homeserver
    element = ScriptedElement(endpoint)
    element.signature = unpadded(bytes(64))
    endpoint.other_device = element

    result = await MatrixE2EE(config, store_path=tmp_path / "store").verify_with_device(
        always_confirm, lambda _message: None, max_wait=5
    )

    assert (result.verified_by, result.signed) == ("ELEMENT", False)


async def test_emoji_verification_answers_a_request_received_while_starting(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.other_device = ScriptedElement(endpoint)
    endpoint.to_device_batches = endpoint.long_poll_batches
    endpoint.long_poll_batches = []

    result = await MatrixE2EE(config, store_path=tmp_path / "store").verify_with_device(
        always_confirm, lambda _message: None, max_wait=5
    )

    assert (result.verified_by, result.signed) == ("ELEMENT", True)


async def test_emoji_verification_ignores_a_malformed_request(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    element = ScriptedElement(endpoint)
    endpoint.long_poll_batches = [
        [element.event("request", {**element.device.request(), "methods": 5})]
    ]
    notices: list[str] = []

    with pytest.raises(RuntimeError, match="in time"):
        await MatrixE2EE(config, store_path=tmp_path / "store").verify_with_device(
            always_confirm, notices.append, max_wait=0.5
        )

    assert any("does not offer emoji" in notice for notice in notices)
    assert endpoint.sent_to_device == []


async def test_emoji_verification_stops_when_the_key_query_fails(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.other_device = ScriptedElement(endpoint)
    store = tmp_path / "store"
    await MatrixE2EE(config, store_path=store).setup()
    endpoint.failing_key_queries_from = endpoint.key_queries + 2

    with pytest.raises(RuntimeError, match="cannot look up the requesting device"):
        await MatrixE2EE(config, store_path=store).verify_with_device(
            always_confirm, lambda _message: None, max_wait=5
        )

    assert endpoint.sent_to_device == []
    request_batch = next(sync for sync in endpoint.syncs() if "timeout" in sync["query"])
    endpoint.failing_key_queries_from = None
    await MatrixE2EE(config, store_path=store).setup()
    assert endpoint.syncs()[-1]["query"]["since"] == request_batch["query"]["since"]


async def test_unexpected_verification_message_cancels(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    element = ScriptedElement(endpoint)
    endpoint.other_device = element
    transaction = {"transaction_id": element.device.transaction_id}
    endpoint.long_poll_batches[0].append(element.event("accept", transaction))

    with pytest.raises(VerificationCancelledError, match="Unexpected"):
        await MatrixE2EE(config, store_path=tmp_path / "store").verify_with_device(
            always_confirm, lambda _message: None, max_wait=5
        )

    event_type, content = endpoint.sent_to_device[-1]
    assert (event_type, content["code"]) == ("m.key.verification.cancel", "m.unexpected_message")
