"""The answers part of `profile_derivation` (fact profile RFC §4.2, §4.7): appended only when a fact names an answered
point, so the string of every other fact stays what it was."""
from src.domain.fact_profile_fingerprint import DERIVE_VERSION, profile_derivation


def test_an_empty_digest_leaves_the_string_as_today():
    assert profile_derivation("reg", "atl", False, 2026, "") == f"{DERIVE_VERSION}|reg|atl|0|y2026"
    assert profile_derivation("reg", "atl", False, 2026) == profile_derivation("reg", "atl", False, 2026, "")
    assert profile_derivation("reg", "atl", True) == f"{DERIVE_VERSION}|reg|atl|1"


def test_a_digest_is_appended_after_the_recording_year():
    assert profile_derivation("reg", "atl", False, 2026, "abc") == f"{DERIVE_VERSION}|reg|atl|0|y2026|aabc"
    assert profile_derivation("reg", "atl", False, None, "abc") == f"{DERIVE_VERSION}|reg|atl|0|aabc"


def test_two_digests_give_two_strings():
    assert profile_derivation("reg", "atl", False, 2026, "abc") != profile_derivation("reg", "atl", False, 2026, "abd")
