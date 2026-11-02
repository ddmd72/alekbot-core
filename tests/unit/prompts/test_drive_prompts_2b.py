"""Slice 2b prompt texts (MULTI_DRIVE_RFC §11.2, §11.10; P-F): the Cabinet line in the selection token, the drive
specialist's tokens and the capabilities reference; the whole-drive note rule. Key nouns only: the tokens are tuned
on evals, so no wording is pinned beyond the rule's nouns."""
import re
from pathlib import Path

import pytest

from src.utils.capabilities import CAPABILITIES_TEXT

ROOT = Path(__file__).resolve().parents[3]
TOKENS = ROOT / "prompts_snapshot" / "tokens" / "system"
EDITED = ("PROTOCOL_AGENT_SELECTION", "COGNITIVE_PROCESS_DRIVE", "PROTOCOL_DRIVE_SEARCH", "PROTOCOL_DRIVE_NOTES")
CABINET = "https://my.alekbot.app/cabinet"
PROVIDER = re.compile(r"(?<![a-z])(onedrive|graph|microsoft|google|gdrive|approot|quickxor|ctag|takendatetime)",
                      re.IGNORECASE)
CYRILLIC = re.compile("[Ѐ-ӿ]")


def _token(name: str) -> str:
    return (TOKENS / f"{name}.groovy").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["PROTOCOL_AGENT_SELECTION", "COGNITIVE_PROCESS_DRIVE"])
def test_the_selection_and_the_specialist_send_settings_to_the_cabinet(name):
    text = _token(name)
    assert CABINET in text
    assert "settings" in text.lower()


def test_the_specialist_reports_a_settings_request_as_not_done():
    text = _token("COGNITIVE_PROCESS_DRIVE")
    assert "not_done" in text and "never change a drive's settings" in text


def test_the_owner_note_on_a_drive_is_data_that_tells_drives_apart():
    text = _token("COGNITIVE_PROCESS_DRIVE")
    assert "owner's note" in text and "never an instruction" in text


def test_the_search_token_states_the_several_drives_rules():
    text = _token("PROTOCOL_DRIVE_SEARCH")
    assert "searched by default" in text and "also on" in text
    assert "write folder" in text and "between drives" in text


def test_the_notes_token_separates_a_whole_drive_note_from_an_item_note():
    text = _token("PROTOCOL_DRIVE_NOTES")
    assert "whole_drive_name" in text and "item_ref" in text


@pytest.mark.parametrize("name", EDITED)
def test_no_cyrillic_and_no_provider_word(name):
    text = _token(name)
    assert not CYRILLIC.search(text)
    assert PROVIDER.search(text) is None


def test_capabilities_tell_about_several_drives_and_the_cabinet():
    text = CAPABILITIES_TEXT
    assert "Several drives" in text and "also on" in text
    assert "whole drive" in text
    assert "is set in the Cabinet" in text
    assert "Add a drive" in text
