from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ALGORITHM = "m.secret_storage.v1.aes-hmac-sha2"


def unpadded(value: bytes) -> str:
    return base64.b64encode(value).decode().rstrip("=")


def recovery_key(key: bytes) -> str:
    raw = b"\x8b\x01" + key
    parity = 0
    for byte in raw:
        parity ^= byte
    raw += bytes([parity])
    number = int.from_bytes(raw, "big")
    digits = ""
    while number:
        number, digit = divmod(number, 58)
        digits = BASE58[digit] + digits
    return " ".join(digits[index : index + 4] for index in range(0, len(digits), 4))


def encrypt_secret(key: bytes, name: str, plaintext: bytes, iv: bytes) -> dict[str, str]:
    derived = HKDF(algorithm=hashes.SHA256(), length=64, salt=bytes(32), info=name.encode()).derive(
        key
    )
    encryptor = Cipher(algorithms.AES(derived[:32]), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return {
        "iv": unpadded(iv),
        "ciphertext": unpadded(ciphertext),
        "mac": unpadded(hmac.digest(derived[32:], ciphertext, "sha256")),
    }


def key_description(key: bytes, passphrase: dict[str, Any] | None = None) -> dict[str, Any]:
    check = encrypt_secret(key, "", bytes(32), bytes(range(16)))
    description: dict[str, Any] = {"algorithm": ALGORITHM, "iv": check["iv"], "mac": check["mac"]}
    if passphrase is not None:
        description["passphrase"] = passphrase
    return description


def pbkdf2_key(passphrase: str, salt: str, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passphrase.encode(), salt.encode(), iterations, 32)
