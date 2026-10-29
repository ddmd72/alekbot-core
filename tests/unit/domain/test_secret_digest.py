"""Keyed digest and log fingerprint of verify-only secrets (SECRETS_AT_REST_RFC §2.5)."""
import hashlib
import hmac
import re

from src.domain.secret_digest import fingerprint, keyed_digest

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")


# HMAC-SHA256(KEY, b"mcp_code\x00code-1"), hex: the stored doc id of code "code-1" never changes silently.
PIN = "5444f11dc763ef715d6c36fdf0e9876dcb0d0e45edc0b83fd783727324a13b97"


def test_pinned_vector():
    assert keyed_digest(KEY, "mcp_code", "code-1") == PIN
    assert PIN == hmac.new(KEY, b"mcp_code\x00code-1", hashlib.sha256).hexdigest()


def test_purpose_separates_uses():
    assert keyed_digest(KEY, "mcp_code", "code-1") != keyed_digest(KEY, "invite", "code-1")


def test_the_separator_keeps_purpose_and_value_apart():
    assert keyed_digest(KEY, "ab", "c") != keyed_digest(KEY, "a", "bc")


def test_the_key_separates_digests():
    other = bytes(31) + b"\x01"
    assert keyed_digest(KEY, "mcp_code", "code-1") != keyed_digest(other, "mcp_code", "code-1")


def test_the_digest_is_not_the_unkeyed_hash_and_holds_no_raw_value():
    digest = keyed_digest(KEY, "mcp_code", "code-1")
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest != hashlib.sha256(b"code-1").hexdigest() and "code-1" not in digest


def test_fingerprint_is_eight_hex_of_sha256():
    assert fingerprint("code-1") == hashlib.sha256(b"code-1").hexdigest()[:8]
    assert re.fullmatch(r"[0-9a-f]{8}", fingerprint("code-1"))

