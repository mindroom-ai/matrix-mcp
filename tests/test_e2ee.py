from __future__ import annotations

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
from nio import AsyncClient, AsyncClientConfig
from nio.crypto import Olm
from nio.crypto.key_export import decrypt_and_read, encrypt_and_save
from peewee import OperationalError

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.e2ee import (
    MISSING_ROOM_KEY,
    E2EEUnavailableError,
    MatrixE2EE,
    e2ee_lock_path,
    e2ee_store_path,
    undelivered_devices,
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
            events = self.to_device_batches.pop(0) if self.to_device_batches else []
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
        if path == "/_matrix/client/v3/keys/query":
            devices = {DEVICE: self.device_keys} if self.device_keys else {}
            devices.update(self.other_devices)
            return web.json_response({"device_keys": {USER: devices}, "failures": {}})
        if path == "/_matrix/client/v3/keys/claim":
            return web.json_response({"one_time_keys": {}, "failures": {}})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

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


async def test_failed_invitee_lookup_still_encrypts_for_joined_members(
    homeserver: tuple[MatrixMCPConfig, CryptoEndpoint], tmp_path: Path
) -> None:
    config, endpoint = homeserver
    endpoint.invited = ["@bob:example.com"]
    endpoint.members_status = 500
    writer = MatrixE2EE(config, store_path=tmp_path)
    try:
        event_type, _ = await writer.encrypt(ROOM, "m.room.message", {"body": "still sent"})
        assert writer._client is not None  # noqa: SLF001
        assert "@bob:example.com" not in writer._client.rooms[ROOM].users  # noqa: SLF001
    finally:
        await writer.aclose()

    assert event_type == "m.room.encrypted"


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
