"""The two specialist tokens carry the mount rule and the `drives` rule (MULTI_DRIVE_RFC §11.2, slice 2a §5). Key
phrases only: the tokens are tuned on evals, so no wording is pinned beyond the rule's nouns."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOKENS = ROOT / "prompts_snapshot" / "tokens" / "system"
NAMES = ("COGNITIVE_PROCESS_DRIVE", "PROTOCOL_DRIVE_SEARCH")
PROVIDER = re.compile(r"(?<![a-z])(onedrive|graph|microsoft|google|gdrive|approot|quickxor|ctag|takendatetime)",
                      re.IGNORECASE)


def _token(name: str) -> str:
    return (TOKENS / f"{name}.groovy").read_text(encoding="utf-8")


def test_the_cognitive_token_states_the_mount_rule():
    text = _token("COGNITIVE_PROCESS_DRIVE")
    assert "starts with the drive's name" in text
    assert "primary drive" in text
    assert "searched by default" in text   # the drive_state input lists the drives' fields


def test_the_search_token_states_the_drives_rule():
    text = _token("PROTOCOL_DRIVE_SEARCH")
    assert "drives" in text and "never guess" in text
    assert "not searched" in text
    assert "Inbox on the primary drive" in text


@pytest.mark.parametrize("name", NAMES)
def test_no_cyrillic_and_no_provider_word(name):
    text = _token(name)
    assert not re.search("[Ѐ-ӿ]", text)
    assert PROVIDER.search(text) is None
