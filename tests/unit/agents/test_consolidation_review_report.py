"""T6c fix round, agent side: one reader of the final report (S4) and the language read in mirror mode only (S8)."""
import json
from unittest.mock import MagicMock

import pytest

from src.agents.consolidation_agent import ConsolidationAgent, _report_object
from src.ports.llm_port import LLMResponse
from tests.unit.agents.test_consolidation_review_pass import (  # noqa: F401  (the world fixture)
    RU_LABELS, _agent, _run, _stale, _task, world,
)


def parse(text):
    return ConsolidationAgent._parse_operations_report(MagicMock(), text)


# ------------------------------------------------------------------------------------------------ S4

@pytest.mark.parametrize("text", [json.dumps([{"operations": [{"action": "CREATE"}]}]), "42", "null", '"ops"'])
def test_a_report_that_is_valid_json_but_no_object_has_no_operations(text):
    assert parse(text) == []
    assert _report_object(text) is None


def test_prose_around_an_unfenced_object_gives_operations_and_the_language_alike():
    text = 'Done. {"operations": [{"action": "UPDATE", "fact_id": "f1"}], "message_language": "ru"} That is all.'
    assert parse(text) == [{"action": "UPDATE", "fact_id": "f1"}]
    assert _report_object(text)["message_language"] == "ru"


@pytest.mark.parametrize("text, ops", [
    ('```json\n{"operations": [{"action": "CREATE"}]}\n```', [{"action": "CREATE"}]),
    ('{"operations": [{"action": "MERGE"}]}', [{"action": "MERGE"}]),
    ("plain text, nothing parsed", []),
    ('```json\n{"operations": [oops]}\n```', []),
    ('{"message_language": "uk"}', []),
])
def test_the_operations_report_reads_as_before(text, ops):
    assert parse(text) == ops


# ------------------------------------------------------------------------------------------------ S8

async def test_fixed_mode_does_not_read_a_spontaneous_language(world):
    fact = await _stale(world, "plays tennis on Sundays")
    final = LLMResponse(text=json.dumps({"operations": [], "message_language": "ru", "labels": RU_LABELS},
                                        ensure_ascii=False), tool_calls=[])
    agent = _agent(world, [final])

    response = await _run(agent, await _task(world, fact, mirror=False))

    assert response.result["message_language"] is None and response.result["labels"] is None


async def test_mirror_mode_reads_the_same_report(world):
    fact = await _stale(world, "plays tennis on Sundays")
    final = LLMResponse(text=json.dumps({"operations": [], "message_language": "ru", "labels": RU_LABELS},
                                        ensure_ascii=False), tool_calls=[])
    agent = _agent(world, [final])

    response = await _run(agent, await _task(world, fact, mirror=True, owner_messages=("Привет",)))

    assert response.result["message_language"] == "ru" and response.result["labels"] == RU_LABELS
