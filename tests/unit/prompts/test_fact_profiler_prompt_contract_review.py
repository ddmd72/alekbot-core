"""The profiler's quality answer and confirmed-people text against the schema and the snapshot tokens (fact profile RFC
§4.3, §4.5, §4.6). The sha pin and the wording of the amended paragraphs are pinned in the main contract file once the
confirm run accepts them."""
import re
from typing import get_args

import pytest

from src.agents.fact_profiler_agent import response_schema
from src.domain.confirmed_people import PEOPLE_BLOCK_TITLE
from src.domain.fact_profile_answer import Quality, ReviewField
from tests.unit.prompts.test_fact_profiler_prompt_contract import _agent_prompt, _fields_line, _tokens


def test_review_items_are_a_closed_object_with_field_and_mention_both_named_in_their_line():
    items = response_schema()["properties"]["review_items"]["items"]
    assert list(items["properties"]) == ["field", "mention"] and items["required"] == ["field", "mention"]
    assert items["additionalProperties"] is False
    line = _fields_line("review_items")
    assert re.search(r"\bfield\b", line) and re.search(r"\bmention\b", line)


def test_the_quality_and_review_field_enums_equal_the_literals_and_appear_in_their_lines():
    properties = response_schema()["properties"]
    assert properties["quality"]["enum"] == list(get_args(Quality))
    assert properties["review_items"]["items"]["properties"]["field"]["enum"] == list(get_args(ReviewField))
    quality_line, items_line = _fields_line("quality"), _fields_line("review_items")
    assert all(value in quality_line for value in get_args(Quality))
    assert all(value in items_line for value in get_args(ReviewField))


def test_the_cognitive_process_names_the_confirmed_people_and_quality_sections():
    body = _tokens()["COGNITIVE_PROCESS_FACT_PROFILER"][1]
    assert "CONFIRMED PEOPLE — what they are for" in body
    assert "QUALITY — what it is for" in body
    assert "QUALITY (quality, review_items)." in body


@pytest.mark.asyncio
async def test_the_block_title_is_not_in_the_system_prompt_because_the_list_travels_in_the_user_message():
    assert PEOPLE_BLOCK_TITLE not in await _agent_prompt()
