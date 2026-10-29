"""Wire tests of GcpKmsCipherAdapter at the SDK boundary (ADAPTER_WIRE_TESTING.md; SECRETS_AT_REST_RFC §2.2, §8 Cipher).

The KMS async client's `encrypt` / `decrypt` are replaced by capturing functions that answer with real
`google.cloud.kms` response types; every request dict is checked by building the real request type from it (an
unknown field name raises) and against the contract rules `KMS_BINDS_KEY_AND_AAD` and `KMS_CRC32C_BOTH_WAYS`. The
real client is never built: the unit network guard trips on `google.auth.default`, so the mocked client is injected.
"""
import base64
import logging
import os
from typing import Any, Callable, Dict, List

import google_crc32c
import pytest
from google.cloud import kms

from src.adapters.gcp_kms_cipher_adapter import GcpKmsCipherAdapter
from tests.contracts.adapter_contracts import KMS_BINDS_KEY_AND_AAD, KMS_CRC32C_BOTH_WAYS

KEY = "projects/p-test/locations/l-test/keyRings/r-test/cryptoKeys/k-test"
AAD = "mcp_clients/client-a"
SECRET = "secret-value-1"


def crc(data: bytes) -> int:
    return int(google_crc32c.value(data))


class CapturingKms:
    """The SDK boundary: records each request and answers with what `answer_*` builds (a plausible service by default)."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.sealed = os.urandom(48)
        self.encrypt_answer: Callable[[Dict[str, Any]], kms.EncryptResponse] = self._encrypt_ok
        self.decrypt_answer: Callable[[Dict[str, Any]], kms.DecryptResponse] = self._decrypt_ok

    def _encrypt_ok(self, request: Dict[str, Any]) -> kms.EncryptResponse:
        return kms.EncryptResponse(name=KEY + "/cryptoKeyVersions/1", ciphertext=self.sealed,
                                   ciphertext_crc32c=crc(self.sealed), verified_plaintext_crc32c=True,
                                   verified_additional_authenticated_data_crc32c=True)

    @staticmethod
    def _decrypt_ok(request: Dict[str, Any]) -> kms.DecryptResponse:
        plain = SECRET.encode()
        return kms.DecryptResponse(plaintext=plain, plaintext_crc32c=crc(plain))

    async def encrypt(self, request: Dict[str, Any]) -> kms.EncryptResponse:
        kms.EncryptRequest(**request)  # every field name and type is the SDK's
        self.calls.append({"op": "encrypt", "request": request})
        return self.encrypt_answer(request)

    async def decrypt(self, request: Dict[str, Any]) -> kms.DecryptResponse:
        kms.DecryptRequest(**request)
        self.calls.append({"op": "decrypt", "request": request})
        return self.decrypt_answer(request)


@pytest.fixture
def sdk() -> CapturingKms:
    return CapturingKms()


@pytest.fixture
def cipher(sdk) -> GcpKmsCipherAdapter:
    return GcpKmsCipherAdapter(KEY, client=sdk)


def contract(request: Dict[str, Any]) -> None:
    KMS_BINDS_KEY_AND_AAD.validate("gcp_kms", {"key_name": KEY, "aad": AAD, "request": request})
    KMS_CRC32C_BOTH_WAYS.validate("gcp_kms", {"key_name": KEY, "aad": AAD, "request": request})


async def test_encrypt_sends_key_aad_and_both_crcs_and_returns_base64_ciphertext(cipher, sdk):
    out = await cipher.encrypt(SECRET, aad=AAD)

    request = sdk.calls[0]["request"]
    contract(request)
    assert request["plaintext"] == SECRET.encode() and request["plaintext_crc32c"] == crc(SECRET.encode())
    assert request["additional_authenticated_data_crc32c"] == crc(AAD.encode())
    assert base64.b64decode(out) == sdk.sealed and SECRET not in out


async def test_decrypt_sends_key_aad_and_both_crcs_and_returns_the_plaintext(cipher, sdk):
    stored = base64.b64encode(sdk.sealed).decode()

    assert await cipher.decrypt(stored, aad=AAD) == SECRET

    request = sdk.calls[0]["request"]
    contract(request)
    assert request["ciphertext"] == sdk.sealed and request["ciphertext_crc32c"] == crc(sdk.sealed)


@pytest.mark.parametrize("flag", ["verified_plaintext_crc32c", "verified_additional_authenticated_data_crc32c"])
async def test_encrypt_raises_when_kms_did_not_verify_a_crc(cipher, sdk, flag):
    def answer(request):
        response = sdk._encrypt_ok(request)
        setattr(response, flag, False)
        return response

    sdk.encrypt_answer = answer
    with pytest.raises(RuntimeError, match="integrity"):
        await cipher.encrypt(SECRET, aad=AAD)


async def test_encrypt_raises_on_a_wrong_ciphertext_crc(cipher, sdk):
    def answer(request):
        response = sdk._encrypt_ok(request)
        response.ciphertext_crc32c = crc(sdk.sealed) ^ 1
        return response

    sdk.encrypt_answer = answer
    with pytest.raises(RuntimeError, match="ciphertext CRC32C"):
        await cipher.encrypt(SECRET, aad=AAD)


async def test_encrypt_raises_when_the_ciphertext_crc_is_missing(cipher, sdk):
    def answer(request):
        return kms.EncryptResponse(ciphertext=sdk.sealed, verified_plaintext_crc32c=True,
                                   verified_additional_authenticated_data_crc32c=True)

    sdk.encrypt_answer = answer
    with pytest.raises(RuntimeError, match="ciphertext CRC32C"):
        await cipher.encrypt(SECRET, aad=AAD)


async def test_decrypt_raises_on_a_wrong_plaintext_crc(cipher, sdk):
    sdk.decrypt_answer = lambda request: kms.DecryptResponse(plaintext=SECRET.encode(),
                                                             plaintext_crc32c=crc(SECRET.encode()) ^ 1)
    with pytest.raises(RuntimeError, match="plaintext CRC32C"):
        await cipher.decrypt(base64.b64encode(sdk.sealed).decode(), aad=AAD)


@pytest.mark.parametrize("op", ["encrypt", "decrypt"])
async def test_an_sdk_error_propagates_and_is_logged(cipher, sdk, op, caplog):
    class ServiceDown(Exception):
        pass

    def fail(request):
        raise ServiceDown("permission denied on the key")

    setattr(sdk, f"{op}_answer", fail)
    with caplog.at_level(logging.ERROR), pytest.raises(ServiceDown):
        if op == "encrypt":
            await cipher.encrypt(SECRET, aad=AAD)
        else:
            await cipher.decrypt(base64.b64encode(sdk.sealed).decode(), aad=AAD)
    assert any(f"KMS {op} failed" in r.getMessage() for r in caplog.records)
    assert not any(SECRET in r.getMessage() for r in caplog.records)


async def test_a_ciphertext_that_is_not_base64_raises_before_any_kms_call(cipher, sdk):
    with pytest.raises(ValueError):
        await cipher.decrypt("not base64 !", aad=AAD)
    assert sdk.calls == []


async def test_the_client_is_built_once_on_first_use(monkeypatch, sdk):
    built: List[CapturingKms] = []

    def factory() -> CapturingKms:
        built.append(sdk)
        return sdk

    monkeypatch.setattr("google.cloud.kms.KeyManagementServiceAsyncClient", factory)
    cipher = GcpKmsCipherAdapter(KEY)
    assert built == []  # construction calls no KMS API (the indexer builds its app outside the loop)

    await cipher.encrypt(SECRET, aad=AAD)
    await cipher.decrypt(base64.b64encode(sdk.sealed).decode(), aad=AAD)

    assert len(built) == 1 and [c["op"] for c in sdk.calls] == ["encrypt", "decrypt"]
