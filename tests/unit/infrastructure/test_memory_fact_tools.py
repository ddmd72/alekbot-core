from unittest.mock import AsyncMock

from src.domain.exceptions import MemoryFactsRejected
from src.domain.llm import ToolCall
from src.domain.memory_fact import (
    MAX_FACTS_PER_DRAFT,
    MEMORY_PREVIEW_DELIVERY,
    PROPOSE_MEMORY_FACTS_TOOL,
    MemoryFactDraft,
    ProposedFact,
)
from src.infrastructure.memory_fact_tools import (
    build_propose_memory_facts_tool_declaration,
    make_propose_memory_facts_handler,
)

FACT = {"text": "User's passport expires on 2031-03-04.", "source": "passport.pdf"}


def _call(**kwargs):
    return ToolCall(name=PROPOSE_MEMORY_FACTS_TOOL, args=kwargs)


def test_declaration_shape():
    d = build_propose_memory_facts_tool_declaration()
    assert d["name"] == "propose_memory_facts"
    assert d["parameters"]["required"] == ["facts"]
    items = d["parameters"]["properties"]["facts"]["items"]
    assert set(items["properties"]) == {"text", "source"}
    assert items["required"] == ["text", "source"]


def test_description_explains_consolidation_and_what_never_goes_in():
    text = build_propose_memory_facts_tool_declaration()["description"]
    assert "user's own messages are picked up automatically" in text
    assert "search_memory cannot find them" in text
    assert "nothing is stored until they paste it" in text
    assert "Never procedures" in text


async def test_success_delivers_preview_and_hides_code_and_facts_from_the_model():
    propose = AsyncMock(return_value="ab12")
    handle = make_propose_memory_facts_handler(propose)

    r = await handle(_call(facts=[FACT]))

    assert not r.failed
    propose.assert_awaited_once_with(MemoryFactDraft(facts=[ProposedFact(**FACT)]))
    assert "ab12" not in r.result_str
    assert "$memory save" not in r.result_str
    assert FACT["text"] not in r.result_str
    assert "do not list the facts again" in r.result_str
    assert not r.history_context
    [item] = r.delivery_items
    assert item.type == MEMORY_PREVIEW_DELIVERY
    assert item.data == {"facts": [FACT], "command": "$memory save ab12"}


async def test_invalid_fact_is_a_failed_result_naming_the_entry_and_nothing_proposed():
    propose = AsyncMock()
    handle = make_propose_memory_facts_handler(propose)

    r = await handle(_call(facts=[FACT, {"text": "", "source": "x"}]))

    assert r.failed
    assert "facts.1.text" in r.result_str
    assert "call propose_memory_facts again" in r.result_str
    propose.assert_not_awaited()


async def test_missing_or_non_list_facts_is_rejected():
    propose = AsyncMock()
    handle = make_propose_memory_facts_handler(propose)
    for args in ({}, {"facts": "User is 40."}, {"facts": []}):
        r = await handle(_call(**args))
        assert r.failed
    propose.assert_not_awaited()


async def test_too_many_facts_is_rejected():
    propose = AsyncMock()
    r = await make_propose_memory_facts_handler(propose)(_call(facts=[FACT] * (MAX_FACTS_PER_DRAFT + 1)))
    assert r.failed and "split" in r.result_str
    propose.assert_not_awaited()


async def test_security_rejection_is_a_failed_result_with_reason():
    propose = AsyncMock(side_effect=MemoryFactsRejected("fact 1 text flagged by the security check"))
    r = await make_propose_memory_facts_handler(propose)(_call(facts=[FACT]))
    assert r.failed
    assert "fact 1 text flagged" in r.result_str
    assert not r.delivery_items


async def test_store_failure_is_a_generic_failed_result():
    propose = AsyncMock(side_effect=RuntimeError("firestore down"))
    r = await make_propose_memory_facts_handler(propose)(_call(facts=[FACT]))
    assert r.failed
    assert "nothing was saved" in r.result_str
    assert "firestore" not in r.result_str
