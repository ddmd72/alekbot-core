"""The tasks MCP resource's two token drafts in the snapshot mirror (EXTERNAL_TASK_QUEUE_RFC §5.4): they parse as the
upload script and the resource read them, cover the contract exactly, keep every tool text within the client's limit,
carry exactly the placeholders code fills, state no example and no number a cap lives in code for, and no profile or
blueprint uses them.

The wording is tuned live (it changes by token upload), so these checks are structural only; no test here pins a
sentence."""
import os
import re
from string import Formatter

import pytest

from firestore_utils import snapshot_serializer as serializer
from src.domain import external_task_mcp_contract as contract
from src.domain.connector_texts import MAX_TOOL_DESCRIPTION_CHARS, ConnectorTexts, parse_keyed_sections

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts_snapshot")
TOOL_TOKEN = "MCP_TASK_TOOL_TEXTS"
RESULT_TOKEN = "MCP_TASK_RESULT_TEXTS"
TOKENS = (TOOL_TOKEN, RESULT_TOKEN)
# The owner's rule for model-facing text: it explains, then rules; no examples (a model copies them literally).
EXAMPLE_WORDS = ("example", "e.g.", "for instance", "such as")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _token(name):
    return serializer.token_from_file(_read("tokens", "system", f"{name}.groovy"))


def _texts(name):
    return parse_keyed_sections(_token(name)["content"])


def _placeholders(text):
    return {field for _, field, _, _ in Formatter().parse(text) if field is not None}


@pytest.mark.parametrize("name", TOKENS)
def test_each_draft_parses_with_the_mirrors_frontmatter(name):
    doc = _token(name)
    assert doc["token_id"] == name
    assert doc["category"] == "mcp_tasks" and doc["class"] == "mcp_texts"
    assert doc["metadata"]["description"].strip()
    assert doc["metadata"]["validation"]["risk_level"] == "SAFE"
    assert doc["source_file"] == f"firestore_utils/uploads/{name}.json"
    assert serializer.token_from_file(serializer.token_to_file(doc)) == doc


def test_the_drafts_cover_the_contract_exactly():
    tools, results = _texts(TOOL_TOKEN), _texts(RESULT_TOKEN)
    assert set(tools) == contract.REQUIRED_TOOL_KEYS
    assert set(results) == contract.REQUIRED_RESULT_KEYS
    ConnectorTexts.from_bodies(_token(TOOL_TOKEN)["content"], _token(RESULT_TOKEN)["content"],
                               contract.REQUIRED_TOOL_KEYS, contract.REQUIRED_RESULT_KEYS)


def test_every_tool_text_is_within_the_clients_limit():
    assert {k: len(t) for k, t in _texts(TOOL_TOKEN).items() if len(t) > MAX_TOOL_DESCRIPTION_CHARS} == {}


def test_each_result_text_carries_exactly_the_contracts_placeholders():
    wrong = {key: sorted(_placeholders(text)) for key, text in _texts(RESULT_TOKEN).items()
             if _placeholders(text) != contract.RESULT_PLACEHOLDERS.get(key, frozenset())}
    assert wrong == {}


def test_no_tool_text_carries_a_placeholder_or_a_number():
    # descriptions are served as they are; every cap (lease length, renewals, claims, result size) lives in code
    tools = _texts(TOOL_TOKEN)
    assert {key: sorted(_placeholders(text)) for key, text in tools.items() if _placeholders(text)} == {}
    assert {key: re.findall(r"\d+", text) for key, text in tools.items() if re.search(r"\d", text)} == {}


@pytest.mark.parametrize("name", TOKENS)
def test_no_draft_gives_an_example_or_carries_cyrillic(name):
    body = _token(name)["content"]
    assert [word for word in EXAMPLE_WORDS if word in body.lower()] == []
    assert not re.search("[Ѐ-ӿ]", body)


def test_no_profile_or_blueprint_references_either_token():
    hits = []
    for folder in ("profiles", "blueprints"):
        for file in sorted(os.listdir(os.path.join(ROOT, folder))):
            text = _read(folder, file)
            hits.extend(f"{folder}/{file}: {name}" for name in TOKENS if name in text)
    assert hits == []
