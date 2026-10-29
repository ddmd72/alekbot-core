"""The AES-GCM test cipher (SECRETS_AT_REST_RFC §2.7, §8 Cipher): real authenticated encryption, AAD bound."""
import base64
import inspect
import os

import pytest
from cryptography.exceptions import InvalidTag

from src.ports.cipher_port import CipherPort
from tests.support.aes_gcm_cipher import AesGcmTestCipher

KEY = bytes.fromhex("00112233445566778899aabbccddeeff" * 2)
AAD = "mcp_clients/client-a"
SECRET = "secret-value-1"


async def test_round_trip_and_the_ciphertext_hides_the_plaintext():
    cipher = AesGcmTestCipher(KEY)
    sealed = await cipher.encrypt(SECRET, aad=AAD)

    assert SECRET not in sealed and SECRET.encode() not in base64.b64decode(sealed)
    assert await cipher.decrypt(sealed, aad=AAD) == SECRET


async def test_two_encryptions_of_one_value_differ():
    cipher = AesGcmTestCipher(KEY)
    assert await cipher.encrypt(SECRET, aad=AAD) != await cipher.encrypt(SECRET, aad=AAD)


async def test_a_wrong_aad_raises():
    cipher = AesGcmTestCipher(KEY)
    sealed = await cipher.encrypt(SECRET, aad=AAD)
    with pytest.raises(InvalidTag):
        await cipher.decrypt(sealed, aad="mcp_clients/client-b")


async def test_one_flipped_byte_raises():
    cipher = AesGcmTestCipher(KEY)
    raw = bytearray(base64.b64decode(await cipher.encrypt(SECRET, aad=AAD)))
    raw[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        await cipher.decrypt(base64.b64encode(bytes(raw)).decode(), aad=AAD)


async def test_another_key_raises():
    sealed = await AesGcmTestCipher(KEY).encrypt(SECRET, aad=AAD)
    with pytest.raises(InvalidTag):
        await AesGcmTestCipher(os.urandom(32)).decrypt(sealed, aad=AAD)


@pytest.mark.parametrize("method", ["encrypt", "decrypt"])
def test_aad_is_keyword_only_with_no_default_on_the_port_and_the_test_cipher(method):
    for cls in (CipherPort, AesGcmTestCipher):
        param = inspect.signature(getattr(cls, method)).parameters["aad"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is inspect.Parameter.empty


async def test_a_call_without_aad_is_a_type_error():
    cipher = AesGcmTestCipher(KEY)
    with pytest.raises(TypeError):
        await cipher.encrypt(SECRET)  # type: ignore[call-arg]
