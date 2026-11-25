from __future__ import annotations

import pytest
from nio.crypto.sas import Sas as NioSas

from matrix_mcp.sas_verification import SasVerification, VerificationCancelledError
from tests.sas_helpers import InitiatingDevice

USER = "@alice:example.com"
DEVICE = "MCPDEVICE"
DEVICE_KEY = "mcp-ed25519-key"


def responder(other: InitiatingDevice) -> SasVerification:
    return SasVerification(
        user_id=USER,
        device_id=DEVICE,
        device_key=DEVICE_KEY,
        their_device_id=other.device_id,
        their_device_key=other.ed25519,
        transaction_id=other.transaction_id,
    )


def test_verification_matches_the_other_device_step_by_step() -> None:
    other = InitiatingDevice(USER, "ELEMENT")
    verification = responder(other)

    assert verification.ready() == {
        "from_device": DEVICE,
        "methods": ["m.sas.v1"],
        "transaction_id": other.transaction_id,
    }
    accept = verification.receive_start(other.start())
    assert accept["message_authentication_code"] == "hkdf-hmac-sha256.v2"
    assert accept["key_agreement_protocol"] == "curve25519-hkdf-sha256"
    assert accept["short_authentication_string"] == ["emoji", "decimal"]
    key = verification.receive_key(other.key())
    emoji_indices, decimals = other.receive_key(accept, key, DEVICE)

    assert verification.emoji == [NioSas.emoji[index] for index in emoji_indices]
    assert verification.decimals == decimals
    other.check_mac(verification.confirm(), DEVICE, DEVICE_KEY)
    assert not verification.finished
    verification.receive_mac(other.mac(DEVICE))
    assert verification.finished
    assert verification.done() == {"transaction_id": other.transaction_id}


def test_mac_received_before_the_user_confirms_still_counts() -> None:
    other = InitiatingDevice(USER, "ELEMENT")
    verification = responder(other)
    accept = verification.receive_start(other.start())
    other.receive_key(accept, verification.receive_key(other.key()), DEVICE)

    verification.receive_mac(other.mac(DEVICE))
    assert verification.their_mac_verified
    verification.confirm()

    assert verification.finished


def test_a_mac_for_another_device_key_cancels() -> None:
    other = InitiatingDevice(USER, "ELEMENT")
    verification = responder(other)
    accept = verification.receive_start(other.start())
    other.receive_key(accept, verification.receive_key(other.key()), DEVICE)

    with pytest.raises(VerificationCancelledError) as cancelled:
        verification.receive_mac(other.mac(DEVICE, device_key="forged-key"))

    assert cancelled.value.code == "m.key_mismatch"
    assert not verification.finished


def test_start_without_the_v2_mac_is_refused() -> None:
    other = InitiatingDevice(USER, "ELEMENT", mac_methods=["hkdf-hmac-sha256"])

    with pytest.raises(VerificationCancelledError) as cancelled:
        responder(other).receive_start(other.start())

    assert cancelled.value.code == "m.unknown_method"


def test_start_from_another_device_is_refused() -> None:
    other = InitiatingDevice(USER, "ELEMENT")
    start = {**other.start(), "from_device": "INTRUDER"}

    with pytest.raises(VerificationCancelledError, match="Unsupported"):
        responder(other).receive_start(start)


def test_messages_out_of_order_cancel() -> None:
    other = InitiatingDevice(USER, "ELEMENT")
    verification = responder(other)

    with pytest.raises(VerificationCancelledError) as cancelled:
        verification.confirm()
    assert cancelled.value.code == "m.unexpected_message"
    verification.receive_start(other.start())
    verification.receive_key(other.key())
    with pytest.raises(VerificationCancelledError):
        verification.receive_key(other.key())
    with pytest.raises(VerificationCancelledError):
        verification.receive_start(other.start())


def test_key_before_the_start_is_refused_and_the_start_still_works() -> None:
    other = InitiatingDevice(USER, "ELEMENT")
    verification = responder(other)

    with pytest.raises(VerificationCancelledError) as cancelled:
        verification.receive_key(other.key())

    assert cancelled.value.code == "m.unexpected_message"
    assert not verification.emoji
    verification.receive_start(other.start())
