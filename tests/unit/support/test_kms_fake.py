"""The SDK-shaped KMS fake (tests/support/kms_fake.py) behaves like the service under the real adapter."""
import base64

import pytest

from src.adapters.gcp_kms_cipher_adapter import GcpKmsCipherAdapter
from tests.support import kms_fake

KEY = "projects/p-test/locations/l-test/keyRings/r-test/cryptoKeys/k-test"
AAD = "mcp_clients/client-a"


async def test_the_adapter_round_trips_over_the_installed_fake(monkeypatch):
    fake = kms_fake.install(monkeypatch)
    cipher = GcpKmsCipherAdapter(KEY)

    sealed = await cipher.encrypt("secret-value-1", aad=AAD)

    assert await cipher.decrypt(sealed, aad=AAD) == "secret-value-1"
    assert len(fake.instances) == 1 and [r["op"] for r in fake.instances[0].requests] == ["encrypt", "decrypt"]


async def test_a_wrong_aad_another_key_or_a_flipped_byte_raises(monkeypatch):
    kms_fake.install(monkeypatch)
    cipher = GcpKmsCipherAdapter(KEY)
    sealed = await cipher.encrypt("secret-value-1", aad=AAD)
    raw = bytearray(base64.b64decode(sealed))
    raw[-1] ^= 1

    with pytest.raises(kms_fake.FakeKmsError):
        await cipher.decrypt(sealed, aad="mcp_clients/client-b")
    with pytest.raises(kms_fake.FakeKmsError):
        await GcpKmsCipherAdapter(KEY + "-other").decrypt(sealed, aad=AAD)
    with pytest.raises(kms_fake.FakeKmsError):
        await cipher.decrypt(base64.b64encode(bytes(raw)).decode(), aad=AAD)


async def test_a_request_crc_that_does_not_match_raises(monkeypatch):
    client = kms_fake.install(monkeypatch)()
    with pytest.raises(kms_fake.FakeKmsError, match="plaintext_crc32c"):
        await client.encrypt(request={"name": KEY, "plaintext": b"x", "plaintext_crc32c": 1})
