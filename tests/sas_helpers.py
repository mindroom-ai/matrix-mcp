from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import vodozemac

V2_MAC = "hkdf-hmac-sha256.v2"


def canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def signed_device_keys(user_id: str, device_id: str, account: vodozemac.Account) -> dict[str, Any]:
    keys: dict[str, Any] = {
        "user_id": user_id,
        "device_id": device_id,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {
            f"curve25519:{device_id}": account.curve25519_key.to_base64(),
            f"ed25519:{device_id}": account.ed25519_key.to_base64(),
        },
    }
    signature = account.sign(canonical(keys).encode()).to_base64()
    return {**keys, "signatures": {user_id: {f"ed25519:{device_id}": signature}}}


class InitiatingDevice:
    """The other client's side of a request-based SAS verification, per the Matrix spec."""

    def __init__(
        self,
        user_id: str,
        device_id: str,
        transaction_id: str = "txn-1",
        mac_methods: list[str] | None = None,
    ) -> None:
        self.user_id = user_id
        self.device_id = device_id
        self.account = vodozemac.Account()
        self.transaction_id = transaction_id
        self.mac_methods = mac_methods or [V2_MAC, "hkdf-hmac-sha256"]
        self._sas = vodozemac.Sas()
        self.public_key = self._sas.public_key.to_base64()
        self.established: Any = None
        self.start_content: dict[str, Any] = {}

    @property
    def ed25519(self) -> str:
        return self.account.ed25519_key.to_base64()

    def request(self) -> dict[str, Any]:
        return {
            "from_device": self.device_id,
            "methods": ["m.sas.v1", "m.qr_code.show.v1", "m.reciprocate.v1"],
            "timestamp": 1,
            "transaction_id": self.transaction_id,
        }

    def start(self) -> dict[str, Any]:
        self.start_content = {
            "from_device": self.device_id,
            "method": "m.sas.v1",
            "key_agreement_protocols": ["curve25519-hkdf-sha256", "curve25519"],
            "hashes": ["sha256"],
            "message_authentication_codes": self.mac_methods,
            "short_authentication_string": ["decimal", "emoji"],
            "transaction_id": self.transaction_id,
        }
        return self.start_content

    def key(self) -> dict[str, Any]:
        return {"transaction_id": self.transaction_id, "key": self.public_key}

    def receive_key(
        self, accept: dict[str, Any], key: dict[str, Any], responder_device: str
    ) -> tuple[list[int], tuple[int, int, int]]:
        commitment = hashlib.sha256((key["key"] + canonical(self.start_content)).encode()).digest()
        assert accept["commitment"] == base64.b64encode(commitment).decode().rstrip("=")
        self.established = self._sas.diffie_hellman(
            vodozemac.Curve25519PublicKey.from_base64(key["key"])
        )
        info = (
            f"MATRIX_KEY_VERIFICATION_SAS|{self.user_id}|{self.device_id}|{self.public_key}"
            f"|{self.user_id}|{responder_device}|{key['key']}|{self.transaction_id}"
        )
        sas_bytes = self.established.bytes(info)
        return list(sas_bytes.emoji_indices), sas_bytes.decimals

    def mac(self, responder_device: str, *, device_key: str | None = None) -> dict[str, Any]:
        info = (
            f"MATRIX_KEY_VERIFICATION_MAC{self.user_id}{self.device_id}"
            f"{self.user_id}{responder_device}{self.transaction_id}"
        )
        macs = {
            f"ed25519:{self.device_id}": self.established.calculate_mac(
                device_key or self.ed25519, info + f"ed25519:{self.device_id}"
            ),
            "ed25519:MASTERKEY": self.established.calculate_mac(
                "MASTERKEY", info + "ed25519:MASTERKEY"
            ),
        }
        return {
            "transaction_id": self.transaction_id,
            "mac": macs,
            "keys": self.established.calculate_mac(",".join(sorted(macs)), info + "KEY_IDS"),
        }

    def check_mac(self, mac: dict[str, Any], responder_device: str, responder_key: str) -> None:
        info = (
            f"MATRIX_KEY_VERIFICATION_MAC{self.user_id}{responder_device}"
            f"{self.user_id}{self.device_id}{self.transaction_id}"
        )
        key_id = f"ed25519:{responder_device}"
        assert mac["keys"] == self.established.calculate_mac(key_id, info + "KEY_IDS")
        assert mac["mac"] == {key_id: self.established.calculate_mac(responder_key, info + key_id)}
