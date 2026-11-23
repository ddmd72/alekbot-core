"""The passkey record's two rules (CABINET_DEVICE_SESSIONS_RFC §7.3): its address is the SHA-256 hex of the raw
credential id, and a signature counter may only advance (WebAuthn L3 §7.2 step 21; 0 → 0 is a synced passkey)."""
import hashlib

import pytest

from src.domain.passkey import credential_hash, sign_count_advances


def test_the_address_is_sha256_hex_of_the_raw_credential_id():
    assert credential_hash(b"\x00\x01cred") == hashlib.sha256(b"\x00\x01cred").hexdigest()
    assert len(credential_hash(b"x")) == 64


@pytest.mark.parametrize("stored, presented, advances", [
    (0, 0, True), (0, 1, True), (4, 5, True), (5, 5, False), (5, 4, False), (5, 0, False),
])
def test_the_counter_rule(stored, presented, advances):
    assert sign_count_advances(stored, presented) is advances
