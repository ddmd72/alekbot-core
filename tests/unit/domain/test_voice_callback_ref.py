import base64
import re

import pytest

from src.domain.voice_callback_ref import from_callback_ref, to_callback_ref


def test_a_ciphertext_round_trips():
    raw = bytes([0xFB, 0xFF, 0xBE, 0xFA, 0x01])  # standard base64 holds '+', '/' and '=' padding
    ciphertext = base64.b64encode(raw).decode("ascii")
    assert "+" in ciphertext and "/" in ciphertext and ciphertext.endswith("=")
    assert from_callback_ref(to_callback_ref(ciphertext)) == ciphertext


def test_the_ref_uses_only_url_safe_characters():
    ciphertext = base64.b64encode(bytes(range(256))).decode("ascii")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", to_callback_ref(ciphertext))


@pytest.mark.parametrize("bad", ["", "ab+c", "ab/c", "ab=c", "a b", "ab%2Bc", "abcde"])
def test_a_malformed_ref_raises(bad):
    with pytest.raises(ValueError):
        from_callback_ref(bad)
