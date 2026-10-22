"""The drive specialist's prompt files in the snapshot mirror (DRIVE_SPECIALIST_RFC §5.2, §10.1): they parse as the
upload script reads them, the profile and blueprint fit together, the tokens name only the specialist's tools and
parameters (no Smart-era intent or field), and the decided rules are present word for word."""
import os
import re

import pytest

from firestore_utils import snapshot_serializer as serializer
from src.domain.drive_tool_texts import drive_tool_declarations

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts_snapshot")
TOKENS = ("COGNITIVE_PROCESS_DRIVE", "PROTOCOL_DRIVE_SEARCH", "OUTPUT_FORMAT_DRIVE")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _token(name):
    return serializer.token_from_file(_read("tokens", "system", f"{name}.groovy"))


@pytest.mark.parametrize("name", TOKENS)
def test_each_token_parses_with_the_mirrors_frontmatter(name):
    doc = _token(name)
    assert doc["token_id"] == name
    assert doc["metadata"]["override_by"] == ["SYSTEM", "AGENT"]
    assert doc["metadata"]["validation"]["risk_level"] == "SAFE"
    assert doc["source_file"] == f"firestore_utils/uploads/{name}.json"
    assert doc["content"].strip()


def test_the_profile_and_the_blueprint_fit_together():
    profile = serializer.doc_from_yaml(_read("profiles", "drive.yaml"))
    blueprint = serializer.doc_from_yaml(_read("blueprints", "drive_agent_v1.yaml"))
    assert profile["agent_id"] == "drive" and profile["blueprint_id"] == blueprint["blueprint_id"] == "drive_agent_v1"
    assert blueprint["outer_class"] == "DriveAgent extends Agent"
    assert set(profile["tokens"]) == set(TOKENS)
    assert all(t["non_overridable"] for t in profile["tokens"].values())
    classes = [_token(name)["class"] for name in sorted(TOKENS, key=lambda n: profile["tokens"][n]["order"])]
    assert classes == blueprint["class_order"] == ["cognitive_process", "protocols", "output_format"]


_SMART_ERA = ("drive_query", "drive_folder", "drive_kind", "drive_date", "drive_deleted", "search_files_in_drive",
              "list_files_in_drive", "open_file_from_drive", "save_file_to_drive", "move_file_in_drive",
              "update_file_in_drive", "delete_file_from_drive", "create_folder_in_drive", "delegate_to_specialist",
              "use_skill", "drive-search", "`original`", "work_with_drive_files")


@pytest.mark.parametrize("name", TOKENS)
def test_tokens_name_no_smart_era_intent_or_field(name):
    body = _token(name)["content"]
    for word in _SMART_ERA:
        assert word not in body, word


_BLOCK_KEYS = {"does_not_own", "execute_as_given", "drive_search", "what_the_drive_holds", "find_or_value",
               "writing_the_search", "reading_the_results", "card_accuracy", "when_nothing_is_found",
               "answering_from_documents", "deleted_files", "plain_text"}
_STATE_KEYS = {"drive_state", "user_message", "file_contents"}


def test_every_identifier_in_the_tokens_is_a_tool_a_parameter_or_a_block_key():
    declarations = drive_tool_declarations()
    tools = {d["name"] for d in declarations}
    params = {p for d in declarations for p in d["parameters"]["properties"]}
    body = "\n".join(_token(name)["content"] for name in TOKENS)
    identifiers = set(re.findall(r"(?<![\w.])[a-z]+(?:_[a-z]+)+(?![\w])", body))
    assert identifiers - tools - params - _BLOCK_KEYS - _STATE_KEYS == set()


def test_the_decided_rules_are_word_for_word():
    search = _token("PROTOCOL_DRIVE_SEARCH")["content"]
    assert ("A recording may be audio or video (.webm is video). Dates in file names appear as YYYY-MM-DD: search "
            "the date in that form too. When a search does not find a recording, listing its media folder is a valid "
            "fallback.") in search
    assert "in ONE language, as the user wrote them. No synonym lists, no translations" in search   # no variant re-search
    process = _token("COGNITIVE_PROCESS_DRIVE")["content"]
    assert "never open a file to confirm a search result" in process
    assert "execute_as_given" in process and "report_to_orchestrator comes in a later turn" in process
    output = _token("OUTPUT_FORMAT_DRIVE")["content"]
    assert "never that something is complete or that nothing else exists" in output
