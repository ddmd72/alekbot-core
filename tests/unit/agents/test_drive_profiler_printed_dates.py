"""The profiler returns each date's `printed` form and the two identity value labels (USER_DRIVE_INDEXING_RFC §26.4
first sentence, §26.5 Δ63). Grading itself is not here."""
from datetime import date

from src.agents.drive_profiler_agent import AGENT_TYPE, DriveProfilerAgent, _RESPONSE_SCHEMA
from src.domain.agent import AgentConfig
from src.domain.drive_enrichment import ProfileDate
from src.domain.drive_enrichment_calls import ProfileInput, ProfileRequest
from src.domain.drive_labels import VALUE_LABELS
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply
from tests.unit.agents.test_drive_profiler_agent import ANSWER


def _agent(script):
    return DriveProfilerAgent(AgentConfig(agent_id="drive_profiler_u1", agent_type=AGENT_TYPE),
                              context(ScriptedLLM(script)), prompt_builder())


def _request():
    return ProfileRequest(user_id="u1", account_id="acc-1", item_id="item-1", kind="pdf", name="card.pdf",
                          input=ProfileInput(head="Valid until 07 05 2031", tail="", headings=()))


def test_the_date_schema_requires_printed():
    items = _RESPONSE_SCHEMA["properties"]["dates"]["items"]
    assert "printed" in items["required"] and items["properties"]["printed"]["type"] == "string"


def test_the_value_labels_offer_the_two_identity_numbers():
    offered = _RESPONSE_SCHEMA["properties"]["key_values"]["items"]["properties"]["label"]["enum"]
    assert offered == list(VALUE_LABELS)
    assert "document_number" in offered and "personal_id_number" in offered


async def test_the_parse_keeps_the_printed_form():
    answer = dict(ANSWER, dates=[{"label": "valid_until", "date": "2031-05-07", "printed": "07 05 2031"}])
    out = await _agent([reply(answer)]).profile(_request())
    assert out.profile.dates == (ProfileDate("valid_until", date(2031, 5, 7), "07 05 2031"),)


async def test_a_date_without_printed_parses_with_an_empty_form():
    answer = dict(ANSWER, dates=[{"label": "issued", "date": "2024-03-15"}])
    out = await _agent([reply(answer)]).profile(_request())
    assert out.profile.dates == (ProfileDate("issued", date(2024, 3, 15), ""),)
