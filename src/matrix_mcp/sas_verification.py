"""Responder side of interactive emoji verification (SAS) with another of the user's devices."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import vodozemac
from nio.crypto.sas import Sas as _NioSas

from matrix_mcp.cross_signing import canonical_json

SAS_METHOD = "m.sas.v1"
_KEY_AGREEMENT = "curve25519-hkdf-sha256"
_HASH = "sha256"
_MAC_METHOD = "hkdf-hmac-sha256.v2"
_EMOJI: list[tuple[str, str]] = list(_NioSas.emoji)
_UNEXPECTED_START = ("m.unexpected_message", "Unexpected start message")
_UNKNOWN_METHOD = ("m.unknown_method", "Unsupported verification method")
_UNEXPECTED_KEY = ("m.unexpected_message", "Unexpected key message")
_INVALID_KEY = ("m.invalid_message", "Invalid public key")
_NOTHING_TO_CONFIRM = ("m.unexpected_message", "No emoji to confirm yet")
_MISSING_DEVICE_MAC = ("m.key_mismatch", "The device key was not confirmed")
_KEY_MISMATCH = ("m.key_mismatch", "Mismatched device keys")
MISMATCHED_SAS = ("m.mismatched_sas", "The emoji did not match")
UNEXPECTED_MESSAGE = ("m.unexpected_message", "Unexpected verification message")


class VerificationCancelledError(RuntimeError):
    """The verification stopped; code and reason are sent to the other device."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class SasVerification:
    """One verification answered by this device, after the other device's request."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        user_id: str,
        device_id: str,
        device_key: str,
        their_device_id: str,
        their_device_key: str,
        transaction_id: str,
    ) -> None:
        self.user_id = user_id
        self.device_id = device_id
        self.device_key = device_key
        self.their_device_id = their_device_id
        self.their_device_key = their_device_key
        self.transaction_id = transaction_id
        self._sas: vodozemac.Sas | None = vodozemac.Sas()
        self._public_key = self._sas.public_key.to_base64()
        self._accepted = False
        self._established: Any = None
        self._their_mac: dict[str, Any] | None = None
        self.emoji: list[tuple[str, str]] = []
        self.decimals: tuple[int, int, int] | None = None
        self.confirmed = False
        self.their_mac_verified = False

    def ready(self) -> dict[str, Any]:
        return {
            "from_device": self.device_id,
            "methods": [SAS_METHOD],
            "transaction_id": self.transaction_id,
        }

    def receive_start(self, content: dict[str, Any]) -> dict[str, Any]:
        """Answer the other device's start with our accept message."""
        if self._accepted:
            raise VerificationCancelledError(*_UNEXPECTED_START)
        if (
            content.get("method") != SAS_METHOD
            or content.get("from_device") != self.their_device_id
        ):
            raise VerificationCancelledError(*_UNKNOWN_METHOD)
        offered = _list(content, "short_authentication_string")
        strings = [method for method in ("emoji", "decimal") if method in offered]
        if (
            _KEY_AGREEMENT not in _list(content, "key_agreement_protocols")
            or _HASH not in _list(content, "hashes")
            or _MAC_METHOD not in _list(content, "message_authentication_codes")
            or not strings
        ):
            raise VerificationCancelledError(*_UNKNOWN_METHOD)
        commitment = hashlib.sha256((self._public_key + canonical_json(content).decode()).encode())
        self._accepted = True
        return {
            "transaction_id": self.transaction_id,
            "key_agreement_protocol": _KEY_AGREEMENT,
            "hash": _HASH,
            "message_authentication_code": _MAC_METHOD,
            "short_authentication_string": strings,
            "commitment": _unpadded(commitment.digest()),
        }

    def receive_key(self, content: dict[str, Any]) -> dict[str, Any]:
        """Derive the short authentication string and answer with our public key."""
        their_key = content.get("key")
        if not self._accepted or self._sas is None or not isinstance(their_key, str):
            raise VerificationCancelledError(*_UNEXPECTED_KEY)
        sas, self._sas = self._sas, None
        try:
            self._established = sas.diffie_hellman(
                vodozemac.Curve25519PublicKey.from_base64(their_key)
            )
        except Exception as exc:
            raise VerificationCancelledError(*_INVALID_KEY) from exc
        info = (
            f"MATRIX_KEY_VERIFICATION_SAS|{self.user_id}|{self.their_device_id}|{their_key}"
            f"|{self.user_id}|{self.device_id}|{self._public_key}|{self.transaction_id}"
        )
        sas_bytes = self._established.bytes(info)
        self.emoji = [_EMOJI[index] for index in sas_bytes.emoji_indices]
        self.decimals = sas_bytes.decimals
        if self._their_mac is not None:
            self._check_their_mac()
        return {"transaction_id": self.transaction_id, "key": self._public_key}

    def confirm(self) -> dict[str, Any]:
        """The user saw the same emoji on both devices: send our MAC."""
        if self._established is None:
            raise VerificationCancelledError(*_NOTHING_TO_CONFIRM)
        self.confirmed = True
        info = self._mac_info(sender=self.device_id, receiver=self.their_device_id)
        key_id = f"ed25519:{self.device_id}"
        return {
            "transaction_id": self.transaction_id,
            "mac": {key_id: self._mac(self.device_key, info + key_id)},
            "keys": self._mac(key_id, info + "KEY_IDS"),
        }

    def receive_mac(self, content: dict[str, Any]) -> None:
        self._their_mac = content
        if self._established is not None:
            self._check_their_mac()

    @property
    def finished(self) -> bool:
        return self.confirmed and self.their_mac_verified

    def done(self) -> dict[str, Any]:
        return {"transaction_id": self.transaction_id}

    def _check_their_mac(self) -> None:
        content = self._their_mac or {}
        macs, keys = content.get("mac"), content.get("keys")
        key_id = f"ed25519:{self.their_device_id}"
        if not isinstance(macs, dict) or not isinstance(keys, str) or key_id not in macs:
            raise VerificationCancelledError(*_MISSING_DEVICE_MAC)
        info = self._mac_info(sender=self.their_device_id, receiver=self.device_id)
        expected_keys = self._mac(",".join(sorted(macs)), info + "KEY_IDS")
        expected_device = self._mac(self.their_device_key, info + key_id)
        if not hmac.compare_digest(keys, expected_keys) or not hmac.compare_digest(
            str(macs[key_id]), expected_device
        ):
            raise VerificationCancelledError(*_KEY_MISMATCH)
        self.their_mac_verified = True

    def _mac_info(self, *, sender: str, receiver: str) -> str:
        return (
            f"MATRIX_KEY_VERIFICATION_MAC{self.user_id}{sender}"
            f"{self.user_id}{receiver}{self.transaction_id}"
        )

    def _mac(self, value: str, info: str) -> str:
        return str(self._established.calculate_mac(value, info))


def _list(content: dict[str, Any], key: str) -> list[Any]:
    value = content.get(key)
    return value if isinstance(value, list) else []


def _unpadded(value: bytes) -> str:
    return base64.b64encode(value).decode().rstrip("=")
