"""Secret storage and cross-signing primitives for verifying this device."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SECRET_STORAGE_ALGORITHM = "m.secret_storage.v1.aes-hmac-sha2"  # noqa: S105
SELF_SIGNING_SECRET = "m.cross_signing.self_signing"  # noqa: S105
_RECOVERY_KEY_PREFIX = b"\x8b\x01"
_RECOVERY_KEY_LENGTH = 35
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_KEY_BYTES = 32
_MAX_PBKDF2_ITERATIONS = 10_000_000


class SecretStorageError(ValueError):
    """Secret storage could not be unlocked or read."""


def decode_recovery_key(recovery_key: str) -> bytes:
    """Decode a base58 recovery key, as shown by Matrix clients, into its 32-byte key."""
    number = 0
    for character in "".join(recovery_key.split()):
        digit = _BASE58_ALPHABET.find(character)
        if digit < 0:
            msg = "The recovery key contains a character that is not base58"
            raise SecretStorageError(msg)
        number = number * 58 + digit
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    parity = 0
    for byte in raw:
        parity ^= byte
    if len(raw) != _RECOVERY_KEY_LENGTH or raw[:2] != _RECOVERY_KEY_PREFIX or parity:
        msg = "The recovery key is mistyped or is not a Matrix recovery key"
        raise SecretStorageError(msg)
    return raw[2 : 2 + _KEY_BYTES]


def passphrase_key(passphrase: str, key_description: dict[str, Any]) -> bytes:
    """Derive the secret storage key from its passphrase."""
    params = key_description.get("passphrase")
    if not isinstance(params, dict) or params.get("algorithm") != "m.pbkdf2":
        msg = "This secret storage key has no passphrase; use the recovery key"
        raise SecretStorageError(msg)
    salt, iterations = params.get("salt"), params.get("iterations")
    bits = params.get("bits", 8 * _KEY_BYTES)
    if (
        not isinstance(salt, str)
        or not isinstance(iterations, int)
        or not 0 < iterations <= _MAX_PBKDF2_ITERATIONS
        or bits != 8 * _KEY_BYTES
    ):
        msg = "The secret storage key has invalid passphrase parameters"
        raise SecretStorageError(msg)
    return hashlib.pbkdf2_hmac(
        "sha512", passphrase.encode(), salt.encode(), iterations, dklen=_KEY_BYTES
    )


def check_key(key: bytes, key_description: dict[str, Any]) -> None:
    """Raise unless the key matches the MAC published with the secret storage key."""
    if key_description.get("algorithm") != SECRET_STORAGE_ALGORITHM:
        msg = "The secret storage key uses an unsupported algorithm"
        raise SecretStorageError(msg)
    iv, mac = key_description.get("iv"), key_description.get("mac")
    if not isinstance(iv, str) or not isinstance(mac, str):
        return
    _, expected = _encrypt(key, "", bytes(_KEY_BYTES), _unpadded_b64decode(iv))
    if not hmac.compare_digest(expected, _unpadded_b64decode(mac)):
        msg = "Wrong recovery key or passphrase"
        raise SecretStorageError(msg)


def decrypt_secret(key: bytes, name: str, encrypted: dict[str, Any]) -> str:
    """Decrypt one secret from its account data, after checking its MAC."""
    try:
        iv = _unpadded_b64decode(encrypted["iv"])
        ciphertext = _unpadded_b64decode(encrypted["ciphertext"])
        mac = _unpadded_b64decode(encrypted["mac"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"The stored {name} secret is malformed"
        raise SecretStorageError(msg) from exc
    aes_key, mac_key = _derive_keys(key, name)
    if not hmac.compare_digest(hmac.digest(mac_key, ciphertext, "sha256"), mac):
        msg = f"Wrong recovery key or passphrase, or the stored {name} secret is damaged"
        raise SecretStorageError(msg)
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    try:
        return plaintext.decode()
    except UnicodeDecodeError as exc:
        msg = f"The stored {name} secret is malformed"
        raise SecretStorageError(msg) from exc


def signing_key(secret: str) -> Ed25519PrivateKey:
    """Load an Ed25519 cross-signing key from its stored secret."""
    try:
        seed = _unpadded_b64decode(secret)
    except ValueError as exc:
        msg = "The stored signing key is malformed"
        raise SecretStorageError(msg) from exc
    if len(seed) != _KEY_BYTES:
        msg = "The stored signing key is malformed"
        raise SecretStorageError(msg)
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key(key: Ed25519PrivateKey) -> str:
    """The unpadded base64 public key, as Matrix publishes it."""
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _unpadded_b64encode(raw)


def sign_json(obj: dict[str, Any], key: Ed25519PrivateKey, user_id: str) -> dict[str, Any]:
    """Return the object with a signature from the key added, per the Matrix signing rules."""
    signed_part = {
        name: value for name, value in obj.items() if name not in ("signatures", "unsigned")
    }
    signature = _unpadded_b64encode(key.sign(canonical_json(signed_part)))
    signatures = dict(obj.get("signatures") or {})
    signatures[user_id] = {
        **signatures.get(user_id, {}),
        f"ed25519:{public_key(key)}": signature,
    }
    return {**signed_part, "signatures": signatures}


def signature_is_valid(obj: dict[str, Any], user_id: str, key: str) -> bool:
    """Whether the object carries a valid signature from the user's Ed25519 public key."""
    signature = (obj.get("signatures") or {}).get(user_id, {}).get(f"ed25519:{key}")
    if not isinstance(signature, str):
        return False
    signed_part = {
        name: value for name, value in obj.items() if name not in ("signatures", "unsigned")
    }
    try:
        Ed25519PublicKey.from_public_bytes(_unpadded_b64decode(key)).verify(
            _unpadded_b64decode(signature), canonical_json(signed_part)
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def canonical_json(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _encrypt(key: bytes, name: str, plaintext: bytes, iv: bytes) -> tuple[bytes, bytes]:
    aes_key, mac_key = _derive_keys(key, name)
    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return ciphertext, hmac.digest(mac_key, ciphertext, "sha256")


def _derive_keys(key: bytes, name: str) -> tuple[bytes, bytes]:
    derived = HKDF(
        algorithm=hashes.SHA256(), length=2 * _KEY_BYTES, salt=bytes(_KEY_BYTES), info=name.encode()
    ).derive(key)
    return derived[:_KEY_BYTES], derived[_KEY_BYTES:]


def _unpadded_b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def _unpadded_b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode().rstrip("=")
