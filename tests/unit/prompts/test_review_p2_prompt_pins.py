"""Prompt <-> domain pins for the P2 shapes of the review (card C): the lifecycle token, the review message and the review
tools' schemas say the same thing as the domain's kinds, text keys and fallbacks."""
import re
from typing import get_args

import pytest

from src.agents.consolidation_agent import _REVIEW_HEADER, _TRANSLATIONS_RULE
from src.domain.fact_review_options import OPTIONS
from src.domain.fact_review_question import TEXT_KEYS, UNKNOWN_TEXT, QuestionFallback, QuestionKind
from src.domain.fact_words import MAX_FACT_WORDS
from tests.unit.prompts.test_consolidation_review_vocabulary import LIFECYCLE, PROSE, REVIEW_TOOLS, UPDATE

ASK = REVIEW_TOOLS["ask_owner"]["properties"]
ALL_TEXT_KEYS = sorted({key for keys in TEXT_KEYS.values() for key in keys})  # includes unknown


def _asking_item(prefix):
    block = re.search(r"asking: \[(.*?)\n    \]", LIFECYCLE, re.DOTALL).group(1)
    return next(line.strip() for line in block.splitlines() if line.strip().startswith(f'"{prefix}'))


def test_as_history_and_payment_are_properties_of_the_update_tool():
    assert UPDATE["as_history"]["type"] == "boolean"
    assert UPDATE["payment"]["type"] == "object"
    assert set(UPDATE["payment"]["properties"]) == {"paid", "not_paid"}
    assert UPDATE["payment"]["required"] == ["paid", "not_paid"]
    assert "as_history" in PROSE and "payment=" in PROSE


def test_the_ask_tool_has_a_kind_texts_and_folds_and_no_fallback_date():
    assert set(ASK) == {"fact_id", "question", "texts", "fallback", "folds", "noise"}
    assert ASK["folds"]["type"] == "array" and ASK["folds"]["items"] == {"type": "string"}


def test_the_ask_texts_schema_has_exactly_the_domains_text_keys():
    assert set(ASK["texts"]["properties"]) == set(ALL_TEXT_KEYS) | {UNKNOWN_TEXT}
    for keys in TEXT_KEYS.values():
        assert set(keys) <= set(ASK["texts"]["properties"])


CHAT_KINDS = [kind for (kind, delivery), keys in TEXT_KEYS.items() if delivery == "chat" and keys]


@pytest.mark.parametrize("kind", CHAT_KINDS)
def test_the_token_names_the_text_keys_of_each_kind_as_the_domain_has_them(kind):
    expected = f"{kind} -> {', '.join(TEXT_KEYS[(kind, 'chat')])}"
    assert expected in _asking_item("texts:"), expected
    assert expected in ASK["texts"]["description"]


def test_a_still_true_question_has_no_texts_in_the_token_and_the_schema():
    assert TEXT_KEYS[("still_true", "chat")] == ()
    assert "still_true -> none" in _asking_item("texts:") and "none for still_true" in ASK["texts"]["description"]


def test_the_ask_kinds_the_token_gives_equal_the_domains_question_kinds():
    line = _asking_item("question:")
    named = re.findall(r"\b(still_true|happened|renewed|paid)\b", line)
    assert set(named) == set(get_args(QuestionKind)) == set(OPTIONS)
    assert all(kind in ASK["question"]["description"] for kind in get_args(QuestionKind))


def test_the_fallbacks_the_token_and_the_schema_give_equal_the_domains():
    token = _asking_item("fallback is")
    schema = ASK["fallback"]["description"]
    for fallback in get_args(QuestionFallback):
        assert re.search(rf"\b{fallback}\b", token) and re.search(rf"\b{fallback}\b", schema), fallback
    assert "fallback_date" not in PROSE and "fallback_date" not in ASK


def test_the_text_limit_the_token_and_the_schema_state_is_the_domains():
    assert f"at most {MAX_FACT_WORDS} words" in _asking_item("texts:")
    assert f"at most {MAX_FACT_WORDS} words" in ASK["texts"]["description"]


def test_the_header_and_the_token_agree_on_outcome_three_and_the_checklist_lines():
    item3 = next(line for line in _REVIEW_HEADER.split("\n") if line.startswith("(3) "))
    assert f'"{item3}"' in LIFECYCLE
    checklist = re.search(r"Before any call, write one line per listed fact[^\n]*", _REVIEW_HEADER).group(0)
    assert "history=<evidence|own_text|ask|none>" in checklist and checklist in PROSE
    assert "dated_plan_passed" not in _REVIEW_HEADER and "dated_plan_passed" not in LIFECYCLE


def test_the_token_has_the_invoices_key_after_basis_and_the_history_call():
    keys = re.findall(r"^    ([a-z_]+): [\[{\"]", LIFECYCLE, re.MULTILINE)
    assert keys.index("invoices") == keys.index("basis") + 1
    assert "history: \"update_fact(" in LIFECYCLE and "as_history=true" in LIFECYCLE


def test_the_translations_rule_names_every_text_key_it_asks_a_translation_for():
    assert "<fact_id>:history" in _TRANSLATIONS_RULE
    for key in ALL_TEXT_KEYS:
        assert key in _TRANSLATIONS_RULE, key
    assert "payment text (paid, not_paid)" in _TRANSLATIONS_RULE


def test_the_review_update_description_and_basis_schema_state_the_history_rule():
    basis = UPDATE["basis"]["description"]
    assert "an archive needs reply" in basis and "a history rewrite (as_history) takes evidence, reply or date_passed" in basis
