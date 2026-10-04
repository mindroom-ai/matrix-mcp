"""End-to-end encryption for the locally configured Matrix device.

Each tool call opens the device's Olm store under a file lock, catches up on
room keys delivered as to-device messages, and closes it again. Several
matrix-mcp processes can share one device because only one of them holds the
store at a time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from aiohttp import ClientError
from filelock import AsyncFileLock, Timeout
from nio import (
    AsyncClient,
    AsyncClientConfig,
    JoinedMembersResponse,
    KeysQueryResponse,
    KeysUploadResponse,
    MegolmEvent,
    ShareGroupSessionResponse,
    SyncResponse,
    ToDeviceEvent,
    WhoamiResponse,
)
from nio.exceptions import EncryptionError
from nio.rooms import MatrixRoom
from peewee import PeeweeException

from matrix_mcp.config import default_config_path
from matrix_mcp.http_headers import resolve_http_headers
from matrix_mcp.tls import default_ssl_context

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from nio.crypto import Olm

    from matrix_mcp.config import MatrixMCPConfig

# nio's event validation and crypto logs embed whole events, including decrypted text
# and attachment keys; keep them out of the MCP client's stderr log. Other nio
# loggers, such as HTTP errors, stay as configured.
for _sensitive_logger in ("nio.events", "nio.crypto"):
    logging.getLogger(_sensitive_logger).setLevel(logging.CRITICAL)
_LOGGER = logging.getLogger(__name__)

MISSING_ROOM_KEY = "missing room key"
E2EE_UNSUPPORTED = "end-to-end encryption is not available in this mode"
_UNDECRYPTABLE = "unable to decrypt"
_UNSUPPORTED_EVENT = "unsupported encrypted event"
_OTHER_ROOM = "decrypted event belongs to a different room"
_MAX_CATCH_UP_SYNCS = 10
_LOCK_TIMEOUT_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 3
_DEDICATED_DEVICE_HINT = (
    "log in with `matrix-mcp auth sso` or `matrix-mcp auth password` to create a dedicated device"
)
# Only to-device messages (room keys) and key counts are needed from /sync.
_CATCH_UP_FILTER: dict[str, Any] = {
    "presence": {"types": []},
    "account_data": {"types": []},
    "room": {"rooms": []},
}


def e2ee_store_path(config: MatrixMCPConfig) -> Path:
    key = f"{config.normalized_homeserver}|{config.user_id or ''}|{config.device_id or ''}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return default_config_path().with_name(f"e2ee-{digest}")


def e2ee_lock_path(store_path: Path) -> Path:
    """Lock beside the store, so deleting the store cannot race a session using it."""
    return store_path.with_name(f"{store_path.name}.lock")


class E2EEUnavailableError(RuntimeError):
    """The local device cannot use end-to-end encryption right now."""


@dataclass(frozen=True)
class DecryptedEvent:
    """A decrypted raw event, or the original event and why it stayed encrypted."""

    event: dict[str, Any] = field(repr=False)
    error: str | None = None


@dataclass(frozen=True)
class E2EEStatus:
    device_id: str
    fingerprint: str
    store_path: Path


class RoomCrypto(Protocol):
    async def decrypt(self, room_id: str, raw: dict[str, Any]) -> DecryptedEvent: ...

    async def encrypt(
        self, room_id: str, event_type: str, content: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]: ...


class MatrixE2EE:
    """A locked crypto session for the configured Matrix device, opened on first use."""

    def __init__(
        self,
        config: MatrixMCPConfig,
        *,
        store_path: Path | None = None,
        lock_timeout: float = _LOCK_TIMEOUT_SECONDS,
    ) -> None:
        token = config.access_token_value()
        if not token or not config.user_id or not config.device_id:
            msg = "End-to-end encryption needs a Matrix user, device ID, and access token"
            raise RuntimeError(msg)
        self._config = config
        self._user_id = config.user_id
        self._device_id = config.device_id
        self._access_token = token
        self._store_path = store_path
        self._lock_timeout = lock_timeout
        self._open_lock = asyncio.Lock()
        self._client: AsyncClient | None = None
        self._file_lock: AsyncFileLock | None = None
        self._error: E2EEUnavailableError | None = None

    @property
    def store_path(self) -> Path:
        return self._store_path or e2ee_store_path(self._config)

    async def setup(self) -> E2EEStatus:
        """Publish this device's keys and catch up on room keys."""
        try:
            client = await self._session()
            return E2EEStatus(
                device_id=self._device_id,
                fingerprint=_olm(client).account.identity_keys["ed25519"],
                store_path=self.store_path,
            )
        finally:
            await self.aclose()

    async def import_keys(self, path: Path, passphrase: str) -> None:
        """Import a passphrase-protected room key export from another Matrix client."""
        try:
            client = await self._session()
            try:
                await client.import_keys(str(path), passphrase)
            except (EncryptionError, ValueError, TypeError, KeyError) as exc:
                msg = "Could not import room keys: wrong passphrase or invalid key export file"
                raise ValueError(msg) from exc
        finally:
            await self.aclose()

    async def decrypt(self, room_id: str, raw: dict[str, Any]) -> DecryptedEvent:
        try:
            client = await self._session()
        except E2EEUnavailableError as exc:
            return DecryptedEvent(raw, str(exc))
        return _decrypt_with(client, room_id, raw)

    async def encrypt(
        self, room_id: str, event_type: str, content: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        try:
            client = await self._session()
        except E2EEUnavailableError as exc:
            msg = f"Cannot encrypt for this room: {exc}"
            raise RuntimeError(msg) from exc
        olm = _olm(client)
        room = client.rooms.get(room_id)
        if room is None:
            room = MatrixRoom(room_id, self._user_id, encrypted=True)
            client.rooms[room_id] = room
        members = await client.joined_members(room_id)
        if not isinstance(members, JoinedMembersResponse):
            msg = f"Matrix joined_members failed: {members}"
            raise RuntimeError(msg)  # noqa: TRY004 - Upstream error response.
        # Refresh every member's devices so new devices receive the room key.
        olm.users_for_key_query.update(room.users)
        keys = await client.keys_query()
        if not isinstance(keys, KeysQueryResponse):
            msg = f"Matrix keys query failed: {keys}"
            raise RuntimeError(msg)  # noqa: TRY004 - Upstream error response.
        if olm.should_share_group_session(room_id):
            shared = await client.share_group_session(room_id, ignore_unverified_devices=True)
            if not isinstance(shared, ShareGroupSessionResponse):
                msg = f"Matrix room key sharing failed: {shared}"
                raise RuntimeError(msg)
            # nio ignores failed to-device sends; a message nobody can decrypt must not go out.
            undelivered = undelivered_devices(olm, room.users, shared.users_shared_with)
            if undelivered:
                msg = (
                    f"Matrix room key delivery failed for {len(undelivered)} device(s); "
                    "the message was not sent"
                )
                raise RuntimeError(msg)
        encrypted_type, encrypted_content = client.encrypt(room_id, event_type, content)
        return encrypted_type, dict(encrypted_content)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        file_lock, self._file_lock = self._file_lock, None
        try:
            if client is not None:
                await _close_client(client)
        finally:
            if file_lock is not None:
                await file_lock.release()

    async def _session(self) -> AsyncClient:
        async with self._open_lock:
            if self._client is not None:
                return self._client
            if self._error is not None:
                raise self._error
            try:
                self._client = await self._connect()
            except E2EEUnavailableError as exc:
                self._error = exc
                raise
            return self._client

    async def _connect(self) -> AsyncClient:
        path = self.store_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"Cannot create the end-to-end encryption store at {path}: {exc}"
            raise E2EEUnavailableError(msg) from exc
        file_lock = AsyncFileLock(e2ee_lock_path(path), timeout=self._lock_timeout)
        try:
            await file_lock.acquire()
        except Timeout as exc:
            msg = "The end-to-end encryption store is in use by another matrix-mcp call; retry"
            raise E2EEUnavailableError(msg) from exc
        except OSError as exc:
            raise _unavailable(exc, path) from exc
        client: AsyncClient | None = None
        try:
            _private_directory(path)
            client = self._new_client(path)
            await self._check_device_identity(client)
            await _upload_keys(client)
            await _catch_up(client)
            await _upload_keys(client)
            await _send_queued_to_device(client)
        except BaseException as exc:
            await _release(client, file_lock)
            if isinstance(exc, E2EEUnavailableError) or not isinstance(exc, Exception):
                raise
            raise _unavailable(exc, path) from exc
        self._file_lock = file_lock
        return client

    async def _check_device_identity(self, client: AsyncClient) -> None:
        """Confirm, before every sync or key upload, that this store owns the device.

        The access token must belong to the configured device, and the homeserver must
        list this store's identity keys for it. Homeservers may overwrite a device's keys
        on upload, and syncing consumes the device's to-device messages, so using another
        client's device would break that client. A brand-new store may publish its keys
        only when the device has none yet.
        """
        olm = _olm(client)
        identity = await client.whoami()
        if not isinstance(identity, WhoamiResponse):
            msg = "Matrix whoami failed; cannot confirm which device the access token belongs to"
            raise E2EEUnavailableError(msg)
        if identity.user_id != self._user_id or identity.device_id != self._device_id:
            msg = (
                f"The access token belongs to device {identity.device_id}, not "
                f"{self._device_id}; {_DEDICATED_DEVICE_HINT}"
            )
            raise E2EEUnavailableError(msg)
        olm.users_for_key_query.add(self._user_id)
        response = await client.keys_query()
        if not isinstance(response, KeysQueryResponse) or response.failures:
            msg = "Matrix key query failed; cannot check this device's published keys"
            raise E2EEUnavailableError(msg)
        published = response.device_keys.get(self._user_id, {}).get(self._device_id)
        if not published:
            if olm.account.shared:
                msg = (
                    "The homeserver no longer lists this device's encryption keys; "
                    f"{_DEDICATED_DEVICE_HINT}"
                )
                raise E2EEUnavailableError(msg)
            return
        keys = published.get("keys", {})
        ours = olm.account.identity_keys
        if (
            keys.get(f"ed25519:{self._device_id}") != ours["ed25519"]
            or keys.get(f"curve25519:{self._device_id}") != ours["curve25519"]
        ):
            msg = (
                "This device's published encryption keys belong to another Matrix client or "
                "to a store removed by `matrix-mcp auth logout`, and matrix-mcp will not use "
                f"or replace them; {_DEDICATED_DEVICE_HINT}"
            )
            raise E2EEUnavailableError(msg)

    def _new_client(self, path: Path) -> AsyncClient:
        config = self._config
        try:
            headers = resolve_http_headers(config.http_headers, config.http_header_commands)
        except (RuntimeError, ValueError):
            msg = "Matrix custom HTTP header resolution failed"
            raise E2EEUnavailableError(msg) from None
        client = _CryptoClient(
            config.normalized_homeserver,
            self._user_id,
            device_id=self._device_id,
            store_path=str(path),
            config=AsyncClientConfig(
                encryption_enabled=True,
                # _catch_up saves the token itself, after room keys are stored.
                store_sync_tokens=False,
                custom_headers=headers or None,
                max_timeouts=_MAX_RETRIES,
                max_limit_exceeded=_MAX_RETRIES,
                request_timeout=_REQUEST_TIMEOUT_SECONDS,
            ),
            # nio annotates ssl as bool but forwards it to aiohttp, which
            # accepts an SSLContext.
            ssl=default_ssl_context(),  # ty: ignore[invalid-argument-type]
        )
        # Loads the Olm account from the store, creating it on first use.
        client.restore_login(
            user_id=self._user_id,
            device_id=self._device_id,
            access_token=self._access_token,
        )
        if client.olm is None:
            msg = "End-to-end encryption support is not installed"
            raise E2EEUnavailableError(msg)
        return client


async def _upload_keys(client: AsyncClient) -> None:
    if not client.should_upload_keys:
        return
    response = await client.keys_upload()
    if not isinstance(response, KeysUploadResponse):
        msg = f"Matrix key upload failed: {response}"
        raise E2EEUnavailableError(msg)


async def _catch_up(client: AsyncClient) -> None:
    """Receive queued room keys; the homeserver delivers to-device messages in batches.

    The sync token acknowledges the batch to the homeserver, so it is saved only after
    nio has processed the batch and stored its room keys.
    """
    store = client.store
    if store is None:
        msg = "End-to-end encryption store is not loaded"
        raise E2EEUnavailableError(msg)
    since = store.load_sync_token()
    olm = _olm(client)
    for _ in range(_MAX_CATCH_UP_SYNCS):
        # nio drops timeout=0 from the request, so the homeserver applies its default
        # long-poll; full_state requests are answered at once. The filter excludes all
        # rooms, so full state adds nothing to the response.
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            response = await client.sync(
                timeout=0,
                since=since,
                sync_filter=_CATCH_UP_FILTER,
                full_state=True,
                set_presence="offline",
            )
        if not isinstance(response, SyncResponse):
            msg = "Matrix sync for end-to-end encryption keys failed"
            raise E2EEUnavailableError(msg)
        since = response.next_batch
        store.save_sync_token(since)
        # nio ignores a count of zero, which would stop one-time key uploads for good
        # once other devices claim them all; an absent count also means zero.
        olm.uploaded_key_count = response.device_key_count.signed_curve25519 or 0
        if not response.to_device_events:
            return
    # Later tool calls continue from the saved token.
    _LOGGER.info("End-to-end encryption catch-up stopped after %s syncs", _MAX_CATCH_UP_SYNCS)


async def _send_queued_to_device(client: AsyncClient) -> None:
    """Send the Olm session repairs nio queues for broken sessions, best effort.

    Repairs need the sender's device keys, so only devices this store already knows can
    be repaired, and a failed repair is retried only if that device sends again.
    """
    try:
        if client.should_claim_keys:
            await client.keys_claim(client.get_users_for_key_claiming())
        await client.send_to_device_messages()
    except (ClientError, TimeoutError):
        _LOGGER.info("Could not send queued Olm session repairs")


class _CryptoClient(AsyncClient):  # type: ignore[misc]  # nio ships no type stubs.
    """An AsyncClient that skips to-device messages it cannot process.

    nio raises on some malformed encrypted to-device messages, which any Matrix user can
    send. Without this, one such message would block every later catch-up.
    """

    def _handle_decrypt_to_device(self, to_device_event: ToDeviceEvent) -> ToDeviceEvent | None:
        try:
            return super()._handle_decrypt_to_device(to_device_event)
        except (PeeweeException, sqlite3.Error, OSError):
            # A local storage failure must stop the catch-up before the batch is acknowledged.
            raise
        except Exception:  # noqa: BLE001 - Skip one bad message, keep the rest of the batch.
            _LOGGER.warning(
                "Skipped an unreadable to-device message from %s", to_device_event.sender
            )
            return None


def undelivered_devices(
    olm: Olm, users: Iterable[str], shared_with: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Devices with an Olm session that should have received the room key but did not.

    Mirrors nio's recipient selection; devices without an Olm session (no one-time keys
    left) cannot receive keys at all, as in other Matrix clients.
    """
    expected = {
        (user_id, device.id)
        for user_id in users
        for device in olm.device_store.active_user_devices(user_id)
        if device.id != olm.device_id
        and not olm.is_device_blacklisted(device)
        and olm.session_store.get(device.curve25519)
    }
    return expected - shared_with


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


async def _release(client: AsyncClient | None, file_lock: AsyncFileLock) -> None:
    try:
        if client is not None:
            await _close_client(client)
    finally:
        await file_lock.release()


def _unavailable(exc: Exception, path: Path) -> E2EEUnavailableError:
    """Degrade reads to decryption errors instead of failing the whole tool call."""
    if isinstance(exc, ClientError | TimeoutError):
        return E2EEUnavailableError(
            "Matrix end-to-end encryption setup failed to reach the homeserver"
        )
    if isinstance(exc, OSError):
        return E2EEUnavailableError(f"Cannot use the end-to-end encryption store at {path}: {exc}")
    return E2EEUnavailableError(f"End-to-end encryption setup failed ({type(exc).__name__})")


async def _close_client(client: AsyncClient) -> None:
    try:
        await client.close()
    finally:
        if client.store is not None:
            client.store.database.close()


def _decrypt_with(  # noqa: PLR0911 - One exit per reason an event stays encrypted.
    client: AsyncClient, room_id: str, raw: dict[str, Any]
) -> DecryptedEvent:
    # nio raises arbitrary errors on hostile input; neither call writes to the store.
    try:
        event = MegolmEvent.from_dict({**raw, "room_id": room_id})
    except Exception:  # noqa: BLE001 - A malformed event stays encrypted, not fatal.
        return DecryptedEvent(raw, _UNSUPPORTED_EVENT)
    if not isinstance(event, MegolmEvent):
        return DecryptedEvent(raw, _UNSUPPORTED_EVENT)
    olm = _olm(client)
    if olm.inbound_group_store.get(room_id, event.sender_key, event.session_id) is None:
        return DecryptedEvent(raw, MISSING_ROOM_KEY)
    try:
        decrypted = olm.decrypt_megolm_event(event, room_id)
    except Exception:  # noqa: BLE001 - Includes nio's TypeError on non-object payloads.
        return DecryptedEvent(raw, _UNDECRYPTABLE)
    # nio reports payloads its own event schemas reject as BadEvent, but they did decrypt.
    source = getattr(decrypted, "source", None)
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("type"), str)
        or source["type"] == "m.room.encrypted"
        or not isinstance(source.get("content"), dict)
    ):
        return DecryptedEvent(raw, _UNDECRYPTABLE)
    if source.get("room_id") != room_id:
        return DecryptedEvent(raw, _OTHER_ROOM)
    merged = {**raw, "type": source["type"], "content": source["content"]}
    bundled = _bundled_replacement(raw)
    if bundled is not None:
        # Only an encrypted replacement may edit an encrypted message. Any other
        # bundle stays as a bare reference, which makes readers look the edit up.
        replacement = (
            _decrypt_with(client, room_id, bundled)
            if bundled.get("type") == "m.room.encrypted"
            else None
        )
        trusted = replacement is not None and replacement.error is None
        unsigned = raw["unsigned"]
        relations = {
            **unsigned["m.relations"],
            "m.replace": replacement.event
            if trusted and replacement is not None
            else {"event_id": bundled.get("event_id")},
        }
        merged["unsigned"] = {**unsigned, "m.relations": relations}
    return DecryptedEvent(merged)


def _olm(client: AsyncClient) -> Olm:
    if client.olm is None:
        msg = "End-to-end encryption support is not installed"
        raise E2EEUnavailableError(msg)
    return client.olm


def _bundled_replacement(raw: dict[str, Any]) -> dict[str, Any] | None:
    unsigned = raw.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, dict) else None
    replacement = relations.get("m.replace") if isinstance(relations, dict) else None
    return replacement if isinstance(replacement, dict) else None
