from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

if TYPE_CHECKING:
    from collections.abc import Callable

from matrix_mcp.cross_signing import (
    SecretStorageError,
    check_key,
    decode_recovery_key,
    decrypt_secret,
    passphrase_key,
    public_key,
    sign_json,
    signature_is_valid,
    signing_key,
)
from tests.secret_storage_helpers import (
    encrypt_secret,
    key_description,
    pbkdf2_key,
    recovery_key,
    unpadded,
)

KEY = bytes(range(32))
USER = "@alice:example.com"


def test_recovery_key_decodes_with_or_without_spaces() -> None:
    text = recovery_key(KEY)
    assert decode_recovery_key(text) == KEY
    assert decode_recovery_key(text.replace(" ", "")) == KEY


@pytest.mark.parametrize("mangle", [lambda text: text[:-1], lambda text: text.replace("1", "2")])
def test_mistyped_recovery_key_is_refused(mangle: Callable[[str], str]) -> None:
    text = recovery_key(KEY)
    mangled = mangle(text)
    assert mangled != text
    with pytest.raises(SecretStorageError):
        decode_recovery_key(mangled)


def test_recovery_key_rejects_non_base58_characters() -> None:
    with pytest.raises(SecretStorageError, match="base58"):
        decode_recovery_key("EsT0 abcd")


def test_key_check_accepts_the_right_key_and_refuses_another() -> None:
    description = key_description(KEY)
    check_key(KEY, description)
    with pytest.raises(SecretStorageError, match="Wrong recovery key"):
        check_key(bytes(32), description)


def test_key_check_refuses_unknown_algorithms() -> None:
    with pytest.raises(SecretStorageError, match="algorithm"):
        check_key(KEY, {**key_description(KEY), "algorithm": "m.other"})


def test_passphrase_derives_the_storage_key() -> None:
    params = {"algorithm": "m.pbkdf2", "salt": "salt", "iterations": 1000}
    assert passphrase_key("correct horse", {"passphrase": params}) == pbkdf2_key(
        "correct horse", "salt", 1000
    )
    with pytest.raises(SecretStorageError, match="no passphrase"):
        passphrase_key("correct horse", key_description(KEY))


def test_secret_decrypts_and_detects_tampering() -> None:
    encrypted = encrypt_secret(KEY, "m.cross_signing.self_signing", b"secret", bytes(16))
    assert decrypt_secret(KEY, "m.cross_signing.self_signing", encrypted) == "secret"
    with pytest.raises(SecretStorageError, match="is damaged"):
        decrypt_secret(KEY, "m.cross_signing.master", encrypted)
    with pytest.raises(SecretStorageError, match="malformed"):
        decrypt_secret(KEY, "m.cross_signing.self_signing", {"iv": encrypted["iv"]})


def test_signing_key_round_trips_its_seed() -> None:
    seed = bytes(range(32, 64))
    key = signing_key(unpadded(seed))
    expected = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    assert base64.b64decode(public_key(key) + "=") == expected.public_bytes_raw()
    with pytest.raises(SecretStorageError):
        signing_key(unpadded(seed[:16]))


def test_signature_covers_the_canonical_object_and_keeps_other_signatures() -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes(32))
    device = {
        "user_id": USER,
        "device_id": "DEVICE",
        "keys": {"ed25519:DEVICE": "é"},
        "signatures": {USER: {"ed25519:DEVICE": "self"}},
        "unsigned": {"device_display_name": "laptop"},
    }

    signed = sign_json(device, key, USER)

    assert "unsigned" not in signed
    assert signed["signatures"][USER]["ed25519:DEVICE"] == "self"
    signature = signed["signatures"][USER][f"ed25519:{public_key(key)}"]
    canonical = json.dumps(
        {"device_id": "DEVICE", "keys": {"ed25519:DEVICE": "é"}, "user_id": USER},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    verifier = Ed25519PublicKey.from_public_bytes(key.public_key().public_bytes_raw())
    verifier.verify(base64.b64decode(signature + "=="), canonical)


@pytest.mark.parametrize(
    "change",
    [{"iterations": 10**12}, {"iterations": 0}, {"bits": 2**40}, {"bits": 512}],
)
def test_passphrase_parameters_out_of_bounds_are_refused(change: dict[str, int]) -> None:
    params = {"algorithm": "m.pbkdf2", "salt": "salt", "iterations": 1000, **change}

    with pytest.raises(SecretStorageError, match="invalid passphrase parameters"):
        passphrase_key("correct horse", {"passphrase": params})


def test_wrong_key_without_a_key_check_is_reported_as_such() -> None:
    description = {"algorithm": key_description(KEY)["algorithm"]}
    other = bytes(32)
    check_key(other, description)
    encrypted = encrypt_secret(KEY, "m.cross_signing.self_signing", b"secret", bytes(16))

    with pytest.raises(SecretStorageError, match="Wrong recovery key or passphrase"):
        decrypt_secret(other, "m.cross_signing.self_signing", encrypted)


def test_signature_check_accepts_only_a_valid_signature() -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    device = {"user_id": USER, "device_id": "DEVICE", "keys": {"ed25519:DEVICE": "abc"}}
    signed = sign_json(device, key, USER)
    key_id = f"ed25519:{public_key(key)}"

    assert signature_is_valid(signed, USER, public_key(key))
    assert not signature_is_valid(device, USER, public_key(key))
    assert not signature_is_valid({**signed, "device_id": "OTHER"}, USER, public_key(key))
    forged = {**signed, "signatures": {USER: {key_id: unpadded(bytes(64))}}}
    assert not signature_is_valid(forged, USER, public_key(key))
    malformed = {**signed, "signatures": {USER: {key_id: "not base64!"}}}
    assert not signature_is_valid(malformed, USER, public_key(key))
