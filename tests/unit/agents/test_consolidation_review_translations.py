"""The review pass's translations (quote translations): the request in both modes and the validated report."""
import json
import logging

from src.domain.agent import AgentStatus
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.fact_review_task import FactReviewTask
from src.ports.llm_port import LLMResponse
from tests.unit.agents.test_consolidation_review_pass import _agent, _run, _stale, _task, _user_text, world  # noqa: F401

PLAYS = "plays tennis on Sundays"
PLAYS_UK = "грає в теніс по неділях"


def _report(**fields):
    return LLMResponse(text=json.dumps({"operations": [], **fields}, ensure_ascii=False), tool_calls=[])


async def _fixed(world, fact, language):
    task = await _task(world, fact, mirror=False)
    return FactReviewTask.model_validate({**task.model_dump(), "language": language})


async def test_fixed_mode_in_another_language_asks_for_translations_of_every_fact(world):
    fact = await _stale(world, PLAYS)
    agent = _agent(world, [_report()])

    await _run(agent, await _fixed(world, fact, "uk"))

    text = _user_text(agent)
    assert "TRANSLATIONS —" in text and "`uk`" in text
    assert "every listed fact" in text and "not as you rewrote it" in text
    assert "owner_messages" not in text and "message_language" not in text and "labels" not in text


async def test_fixed_mode_in_english_asks_for_no_translations(world):
    fact = await _stale(world, PLAYS)
    agent = _agent(world, [_report()])

    await _run(agent, await _fixed(world, fact, "en"))
    await_text = _user_text(agent)
    assert "TRANSLATIONS" not in await_text and "translations" not in await_text


async def test_fixed_mode_without_a_language_is_the_message_as_before(world):
    fact = await _stale(world, PLAYS)
    agent = _agent(world, [_report()])

    await _run(agent, await _task(world, fact, mirror=False))

    assert "TRANSLATIONS" not in _user_text(agent)


async def test_mirror_mode_also_asks_for_translations_into_the_reported_language(world):
    fact = await _stale(world, PLAYS)
    agent = _agent(world, [_report()])

    await _run(agent, await _task(world, fact, mirror=True, owner_messages=("Привіт",)))

    text = _user_text(agent)
    assert "If message_language is not en, also give `translations`" in text
    assert "TRANSLATIONS — `translations` is one object" in text
    assert "owner_messages" in text and "`labels`" in text


async def test_a_fixed_mode_report_returns_valid_translations_tagged_with_the_fixed_language(world, caplog):
    fact, other = await _stale(world, PLAYS), await _stale(world, "was born in 1990")
    final = _report(translations={fact: PLAYS_UK, other: "народився у 1991", "no-such-id": "щось"})
    agent = _agent(world, [final])
    task = FactReviewTask.model_validate({**(await _task(world, fact, other, mirror=False)).model_dump(), "language": "uk"})

    with caplog.at_level(logging.WARNING):
        response = await _run(agent, task)

    assert response.status == AgentStatus.SUCCESS, response.error
    assert response.result["translations"] == [
        {"language": "uk", "source_digest": quote_digest(PLAYS), "text": PLAYS_UK},
    ]
    warned = " ".join(r.getMessage() for r in caplog.records if "translation refused" in r.getMessage())
    assert other in warned and "no-such-id" in warned
    assert PLAYS_UK not in warned and "1991" not in warned


async def test_a_mirror_report_tags_translations_with_the_reported_language(world):
    fact = await _stale(world, PLAYS)
    agent = _agent(world, [_report(message_language="RU", translations={fact: "играет в теннис по воскресеньям"})])

    response = await _run(agent, await _task(world, fact, mirror=True, owner_messages=("Привет",)))

    (record,) = response.result["translations"]
    assert record["language"] == "ru" and record["source_digest"] == quote_digest(PLAYS)
    assert QuoteTranslation(**record).text == "играет в теннис по воскресеньям"


async def test_a_mirror_report_without_a_language_or_in_english_keeps_none(world):
    fact = await _stale(world, PLAYS)
    for fields in ({"translations": {fact: "x"}}, {"message_language": "en", "translations": {fact: "x"}}):
        agent = _agent(world, [_report(**fields)])
        response = await _run(agent, await _task(world, fact, mirror=True, owner_messages=("Hello",)))
        assert response.result["translations"] == []


async def test_a_report_without_translations_succeeds(world):
    fact = await _stale(world, PLAYS)
    agent = _agent(world, [_report()])

    response = await _run(agent, await _fixed(world, fact, "uk"))

    assert response.status == AgentStatus.SUCCESS and response.result["translations"] == []
