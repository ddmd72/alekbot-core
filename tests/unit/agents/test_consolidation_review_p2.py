"""The review pass, Facts P2 (card C): translations of the texts the pass wrote, the invoices of a history rewrite, the
word count the model is given and the question kind in a fact's line."""
import json
import logging
from datetime import timedelta

from src.domain.agent import AgentStatus
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_quote_translation import quote_digest
from src.domain.fact_review_result import decode_review_result
from src.domain.fact_review_task import FactReviewTask
from src.domain.fact_words import MAX_FACT_WORDS, word_count
from src.ports.llm_port import LLMResponse
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.unit.agents.test_consolidation_review_pass import (  # noqa: F401,F811  (the world fixture)
    USER, _agent, _line, _requests, _run, _stale, _task, _tools, _user_text, world,
)
from tests.support.review_rule_builders import question as build_question

PLAYS = "Pottery class on Nov 4, 2026"
TEXTS = {
    "happened": "The user attended a pottery class on Nov 4, 2026.",
    "not_happened": "The user planned a pottery class on Nov 4, 2026, and did not go.",
    "unknown": "The user planned a pottery class on Nov 4, 2026; whether it took place is unknown.",
}
UK_HAPPENED = "Користувач відвідав заняття з кераміки 4 листопада 2026."
UK_NOT_HAPPENED = "Користувач планував заняття з кераміки 4 листопада 2026 і не пішов."
INVOICE = "An invoice for the phone bill (42.50 EUR), due Oct 20, 2026, was received; payment not confirmed."
PAYMENT = {
    "paid": "The phone bill invoice (42.50 EUR), due Oct 20, 2026, was paid.",
    "not_paid": "The phone bill invoice (42.50 EUR), due Oct 20, 2026, was not paid.",
}


def _report(**fields):
    return LLMResponse(text=json.dumps({"operations": [], **fields}, ensure_ascii=False), tool_calls=[])


async def _fixed_uk(world, *facts):
    task = await _task(world, *facts, mirror=False)
    return FactReviewTask.model_validate({**task.model_dump(), "language": "uk"})


def _ask(fact):
    return ("ask_owner", {"fact_id": fact, "question": "happened", "texts": TEXTS, "fallback": "history"})


async def test_the_translations_of_the_texts_of_an_ask_are_kept_beside_the_facts(world):
    fact = await _stale(world, PLAYS)
    final = _report(translations={
        fact: "Заняття з кераміки 4 листопада 2026",
        f"{fact}:happened": UK_HAPPENED,
        f"{fact}:not_happened": UK_NOT_HAPPENED,
    })
    agent = _agent(world, [_tools(_ask(fact)), final])

    response = await _run(agent, await _fixed_uk(world, fact))

    assert response.status == AgentStatus.SUCCESS, response.error
    kept = {t["source_digest"]: t["text"] for t in response.result["translations"]}
    assert kept == {
        quote_digest(PLAYS): "Заняття з кераміки 4 листопада 2026",
        quote_digest(TEXTS["happened"]): UK_HAPPENED,
        quote_digest(TEXTS["not_happened"]): UK_NOT_HAPPENED,
    }
    assert response.result["asks"][0]["question"] == "happened" and response.result["asks"][0]["texts"] == TEXTS


async def test_the_history_text_of_a_rewrite_is_translated_under_its_history_key(world):
    fact = await _stale(world, PLAYS)
    history = "The user planned a pottery class on Nov 4, 2026, and did not go."
    write = ("update_fact", {
        "fact_id": fact, "updates": {"state": "current", "content": history, "expiry": {"kind": "none"}},
        "basis": {"kind": "date_passed", "date": "2026-11-04"}, "as_history": True,
    })
    final = _report(translations={f"{fact}:history": UK_NOT_HAPPENED})
    agent = _agent(world, [_tools(write), final])
    world.advance(days=30)

    response = await _run(agent, await _fixed_uk(world, fact))

    assert response.status == AgentStatus.SUCCESS, response.error
    assert [t["source_digest"] for t in response.result["translations"]] == [quote_digest(history)]


async def test_a_translation_of_an_unknown_key_or_with_a_changed_number_is_refused(world, caplog):
    fact = await _stale(world, PLAYS)
    final = _report(translations={
        f"{fact}:happened": "Користувач відвідав заняття з кераміки 5 листопада 2026.",   # the 4 became a 5
        f"{fact}:not_happened": UK_NOT_HAPPENED,
        f"{fact}:unknown": "x",                                                          # a text the pass never wrote
        "no-such-id:happened": "щось",
    })
    agent = _agent(world, [_tools(_ask(fact)), final])

    with caplog.at_level(logging.WARNING):
        response = await _run(agent, await _fixed_uk(world, fact))

    assert [t["text"] for t in response.result["translations"]] == [UK_NOT_HAPPENED]
    warned = " ".join(r.getMessage() for r in caplog.records if "translation refused" in r.getMessage())
    assert f"{fact}:happened" in warned and "no-such-id:happened" in warned


async def test_a_text_that_was_never_written_has_no_translation_source(world, caplog):
    fact = await _stale(world, PLAYS)
    agent = _agent(world, [_report(translations={f"{fact}:paid": "Оплачено"})])

    with caplog.at_level(logging.WARNING):
        response = await _run(agent, await _fixed_uk(world, fact))

    assert response.result["translations"] == []
    assert any(f"{fact}:paid: names no listed fact or text this pass wrote" in r.getMessage() for r in caplog.records)


async def test_the_invoices_of_the_pass_reach_the_result(world):
    fact = await world.seed_stale("Phone bill invoice of 42.50 EUR, due Oct 20, 2026", expiry_kind="horizon", ttl_days=14,
                                  reported_date=NOW - timedelta(days=60))
    write = ("update_fact", {
        "fact_id": fact, "updates": {"state": "current", "content": INVOICE, "expiry": {"kind": "none"}},
        "basis": {"kind": "date_passed", "date": "2026-10-20"}, "as_history": True, "payment": PAYMENT,
    })
    agent = _agent(world, [_tools(write), _report()])

    response = await _run(agent, await _task(world, fact))

    assert response.status == AgentStatus.SUCCESS, response.error
    result = decode_review_result(response.result)
    [invoice] = result.invoices
    assert (invoice.fact_id, invoice.paid, invoice.not_paid) == (fact, PAYMENT["paid"], PAYMENT["not_paid"])
    assert invoice.written_id == result.outcomes[0].written_id
    assert result.outcomes[0].as_history is True


async def test_count_words_counts_the_way_the_write_path_does(world):
    fact = await _stale(world, PLAYS)
    long_text = " ".join(["word"] * (MAX_FACT_WORDS + 3))
    agent = _agent(world, [_tools(("count_words", {"text": long_text}), ("count_words", {"text": "a  b\tc"})), _report()])

    await _run(agent, await _task(world, fact))

    sent = json.dumps([str(m) for m in _requests(agent)[1].messages], ensure_ascii=False)
    assert word_count(long_text) == MAX_FACT_WORDS + 3 and word_count("a  b\tc") == 3
    assert f'\\"word_count\\": {MAX_FACT_WORDS + 3}' in sent and '\\"within_limit\\": false' in sent
    assert '\\"word_count\\": 3' in sent and '\\"within_limit\\": true' in sent


async def test_a_facts_question_view_carries_its_kind(world):
    fact = await _stale(world, PLAYS)
    waiting = build_question(
        QuestionStatus.ASKED, fact_id=fact, account_id=ACC, user_id=USER, question="happened", texts=TEXTS,
        fallback="history", reply_text="I went", reply_at=NOW - timedelta(days=1),
    )
    agent = _agent(world, [_report()])

    await _run(agent, await _task(world, fact, questions=(waiting,)))

    question = _line(_user_text(agent))["question"]
    assert list(question)[:2] == ["question_id", "kind"] and question["kind"] == "happened"
    assert question["fallback"] == "history"
