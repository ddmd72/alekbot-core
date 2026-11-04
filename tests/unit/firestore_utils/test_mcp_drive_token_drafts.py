"""The drive connector's two token drafts in the snapshot mirror (DRIVE_MCP_CONNECTOR_RFC §5.2, §5.3, §5.5): they parse
as the upload script and the connector read them, cover the connector's contract exactly, keep every tool text within
the client's limit, carry no Cyrillic and no word addressed to the chat's specialist, format with the same
placeholders as the chat constants they replace, and no profile or blueprint uses them.

The wording is tuned live (D4: it changes by token upload), so these checks are structural only; no test here pins a
sentence."""
import os
import re
from string import Formatter

import pytest

from firestore_utils import snapshot_serializer as serializer
from src.domain import drive_answer_texts as chat
from src.domain import drive_connector_contract as contract
from src.domain.connector_texts import MAX_TOOL_DESCRIPTION_CHARS, ConnectorTexts, parse_keyed_sections
from src.domain.drive_excerpt_render import PAGE_NEXT as CHAT_PAGE_NEXT
from src.domain.document_limits import OPEN_MAX_CHARS
from src.domain.drive_open_texts import ROUTE_SPECIALIST
from src.domain.drive_tool_texts import OPEN_DRIVE_FILE

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts_snapshot")
TOOL_TOKEN = "MCP_DRIVE_TOOL_TEXTS"
RESULT_TOKEN = "MCP_DRIVE_RESULT_TEXTS"
TOKENS = (TOOL_TOKEN, RESULT_TOKEN)

# Words that address the chat's specialist or its orchestrator: an outside client has neither (RFC §5.5).
AUDIENCE_WORDS = ("orchestrator", "commission", "specialist", "drive_state")

# The chat constant each connector result text replaces, where one exists (RFC §5.5): the code formats both alike.
CHAT_COUNTERPARTS = {
    contract.STATUS: chat.STATUS_HEAD,
    contract.WHAT_TO_DO: chat.WHAT_TO_DO,
    contract.ANSWER_FROM_RESULTS: chat.ANSWER_FROM_RESULTS,
    contract.UNFINISHED: chat.UNFINISHED_LINE,
    contract.LANGUAGE: chat.LANGUAGE_LINE,
    contract.ROUTE: ROUTE_SPECIALIST,
    contract.PAGE_NEXT: CHAT_PAGE_NEXT,
    **chat.CHAT_READING_TEXTS,
}


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
    assert doc["category"] == "mcp_drive" and doc["class"] == "mcp_texts"
    assert doc["metadata"]["description"].strip()
    assert doc["metadata"]["validation"]["risk_level"] == "SAFE"
    assert doc["source_file"] == f"firestore_utils/uploads/{name}.json"
    assert serializer.token_from_file(serializer.token_to_file(doc)) == doc
    assert _texts(name)


def test_the_drafts_cover_the_contract_exactly():
    texts = ConnectorTexts.from_bodies(_token(TOOL_TOKEN)["content"], _token(RESULT_TOKEN)["content"],
                                       contract.REQUIRED_TOOL_KEYS, contract.REQUIRED_RESULT_KEYS)
    assert set(texts.tools) == contract.REQUIRED_TOOL_KEYS
    assert set(texts.results) == contract.REQUIRED_RESULT_KEYS


def test_the_contract_holds_every_tool_parameter_and_reading_key():
    assert contract.REQUIRED_TOOL_KEYS == {contract.INSTRUCTIONS_KEY, *contract.TOOL_NAMES,
                                           *(contract.text_key_of_parameter(tool, param)
                                             for tool, params in contract.TOOL_PARAMETERS.items() for param in params)}
    assert set(contract.TOOL_PARAMETERS) == set(contract.TOOL_NAMES)
    assert set(chat.CHAT_READING_TEXTS) <= contract.REQUIRED_RESULT_KEYS


def test_every_tool_text_is_within_the_clients_limit():
    too_long = {key: len(text) for key, text in _texts(TOOL_TOKEN).items() if len(text) > MAX_TOOL_DESCRIPTION_CHARS}
    assert too_long == {}


@pytest.mark.parametrize("name", TOKENS)
def test_each_draft_has_no_cyrillic(name):
    assert not re.search("[Ѐ-ӿ]", _read("tokens", "system", f"{name}.groovy")), name


@pytest.mark.parametrize("name", TOKENS)
def test_no_text_addresses_the_specialist_or_the_orchestrator(name):
    body = _token(name)["content"].lower()
    assert [word for word in AUDIENCE_WORDS if word in body] == []


def test_the_contract_formats_each_result_text_like_the_chat_constant_it_replaces():
    for key, chat_text in CHAT_COUNTERPARTS.items():
        assert contract.RESULT_PLACEHOLDERS.get(key, frozenset()) == _placeholders(chat_text), key


def test_each_result_text_carries_exactly_the_contracts_placeholders():
    wrong = {key: sorted(_placeholders(text)) for key, text in _texts(RESULT_TOKEN).items()
             if _placeholders(text) != contract.RESULT_PLACEHOLDERS.get(key, frozenset())}
    assert wrong == {}


def test_no_tool_text_carries_a_placeholder():
    # descriptions are served as they are: nothing formats them
    assert {key: sorted(_placeholders(text)) for key, text in _texts(TOOL_TOKEN).items() if _placeholders(text)} == {}


# The open bound as the texts state it ("a file over 60,000 characters"), formatted as the chat texts format it.
_OPEN_BOUND = re.compile(r"over ([\d,]+) characters")


def test_every_stated_open_bound_is_the_codes_bound():
    texts = {**_texts(TOOL_TOKEN), **_texts(RESULT_TOKEN)}
    stated = {key: _OPEN_BOUND.findall(text) for key, text in texts.items() if _OPEN_BOUND.search(text)}
    assert {OPEN_DRIVE_FILE, "R_TOO_LONG"} <= set(stated)
    assert {key: numbers for key, numbers in stated.items() if set(numbers) != {f"{OPEN_MAX_CHARS:,}"}} == {}


def test_no_profile_or_blueprint_references_either_token():
    hits = []
    for folder in ("profiles", "blueprints"):
        for file in sorted(os.listdir(os.path.join(ROOT, folder))):
            text = _read(folder, file)
            hits.extend(f"{folder}/{file}: {name}" for name in TOKENS if name in text)
    assert hits == []


# -- DRIVE_MCP_CONNECTOR_RFC §14.4: wide and batch ---------------------------------------------------------------------

WIDE_BATCH_TOOL_KEYS = ("search_drive.wide", "find_passages.wide", "find_passages.batch")


def test_the_drafts_carry_the_wide_and_batch_keys():
    tools, results = _texts(TOOL_TOKEN), _texts(RESULT_TOKEN)
    assert set(WIDE_BATCH_TOOL_KEYS) <= set(tools)
    assert set(contract.PAGING_KEYS) <= set(results)
    assert 'wide "yes"' in tools[contract.INSTRUCTIONS_KEY]   # the method-by-need list names the wide case


def test_the_wide_and_batch_texts_state_no_cap_number():
    # E21: "about twice as many", never the numbers: the caps live in code, the words in the token
    from src.domain.drive_excerpt_caps import ExcerptCaps
    from src.domain.drive_search_caps import SearchCaps
    numbers = {ExcerptCaps.WIDE.k, ExcerptCaps.WIDE.chars, ExcerptCaps.DEFAULT.k, ExcerptCaps.DEFAULT.chars,
               SearchCaps.WIDE.about, SearchCaps.WIDE.result_chars, SearchCaps.DEFAULT.about}
    tools, results = _texts(TOOL_TOKEN), _texts(RESULT_TOKEN)
    texts = {**{k: tools[k] for k in WIDE_BATCH_TOOL_KEYS}, **{k: results[k] for k in contract.PAGING_KEYS}}
    stated = {key: re.findall(r"\d[\d,]*", text) for key, text in texts.items()}
    assert {key: found for key, found in stated.items()
            if {int(n.replace(",", "")) for n in found} & numbers} == {}
