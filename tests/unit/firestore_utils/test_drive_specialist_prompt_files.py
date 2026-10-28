"""The drive specialist's prompt files in the snapshot mirror (DRIVE_SPECIALIST_RFC §5.2, §10.1): they parse as the
upload script reads them and round-trip, the profile and blueprint fit together, the tokens name only the specialist's
tools and parameters (no Smart-era intent or field) and carry no Cyrillic.

Reviewer (owner decision 2026-10-28): the tokens are rewritten while they are tuned on evals, so these checks are
structural only; no test here pins their wording."""
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


@pytest.mark.parametrize("name", TOKENS)
def test_each_token_round_trips_through_the_serializer(name):
    doc = _token(name)
    assert serializer.token_from_file(serializer.token_to_file(doc)) == doc


@pytest.mark.parametrize("name", TOKENS)
def test_each_token_has_no_cyrillic(name):
    assert not re.search("[\u0400-\u04ff]", _read("tokens", "system", f"{name}.groovy")), name


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


_STATE_KEYS = {"drive_state", "user_message", "file_contents"}   # the drive_state block the agent's code renders
# A key of the token's own structure (`name {`, `name: "…"`, `name: [`) is the token's, not a tool or a parameter.
# Reviewer (owner decision 2026-10-28): read from the tokens instead of a fixed list, so a rewrite may rename its blocks.
_KEY = re.compile(r'(?m)^\s*([a-z]+(?:_[a-z]+)+)\s*(?:\{|:\s*["\[])')


def test_every_identifier_in_the_tokens_is_a_tool_a_parameter_or_a_block_key():
    declarations = drive_tool_declarations()
    tools = {d["name"] for d in declarations}
    params = {p for d in declarations for p in d["parameters"]["properties"]}
    body = "\n".join(_token(name)["content"] for name in TOKENS)
    identifiers = set(re.findall(r"(?<![\w.])[a-z]+(?:_[a-z]+)+(?![\w])", body))
    block_keys = set(_KEY.findall(body))
    assert identifiers - tools - params - block_keys - _STATE_KEYS == set()


# Reviewer: test_the_decided_rules_are_word_for_word and test_a_missing_file_contents_item_is_not_reported_as_not_done
# retired: each pinned sentences of the specialist's tokens word for word (owner decision 2026-10-28). The code's
# refusal of a missing file_contents item stays pinned by tests/unit/agents/test_drive_agent_missing_item.py.
