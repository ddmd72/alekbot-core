"""Stage 3 for mail with no readable card (email index v2 §6.7 as changed by slice 9b): an `empty` card is skipped and
marked, and a pass of skipped mail only makes no model call; `abandoned`, `source_gone`, stale and unreadable `carded`
mail go as their headers (an old record's v1 fact never leaves); without the card reads triage is skipped; the pass's
INFO line counts each form. The mail, cards and marks live on the evaluating fake behind the real repositories; the
model is scripted and only the messages it was sent are read."""
import logging
from datetime import datetime, timedelta, timezone

from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import CardExpect, gone_fields, pending_fields, settled_fields
from src.domain.email_memory_candidate import EMAIL_CANDIDATES_HEADER, header_candidate
from tests.support.fact_kit import USER
from tests.unit.agents.test_consolidation_email_cards import (  # noqa: F401  (world is a fixture)
    FACTS, LISBON, OLD, RecordingPrompts, booking_fields, email, make_agent, marks, numbered, run, world,
)
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer_json, mail

NOW = datetime.now(timezone.utc)


def _empty_fields():
    m = mail(subject="This week's offers", from_name="Supermercado Exemplo", from_address="news@supermercado.example",
             from_domain="supermercado.example", email_id="msg_shop03")
    inp = build_card_input(m, "Fresh fruit at half price this week only.", [], [], direct=False, nonce=NONCE)
    card = build_card(parse_mail_answer(answer_json(substance="none", key_values=[], entities=[], dates=[],
                                                    doc_type="newsletter")), m, [], inp.evidence, PEOPLE)
    fields = settled_fields(card, digest="dg", model="flash-lite", now=NOW, vector_pending=False)
    assert fields["card_state"] == "empty"
    return fields


def _abandoned_fields():
    return {**pending_fields(reason="invalid", failures=3, model="flash-lite"), "card_state": "abandoned"}


async def _card(world, email_id, fields):
    await world.cards.write_card(USER, email_id, fields, None, CardExpect.absent())


async def test_an_empty_card_is_skipped_and_marked(world):
    await world.emails.save_batch([email("msg_rome01", OLD), email("msg_shop03", OLD + timedelta(hours=1))])
    await _card(world, "msg_rome01", booking_fields())
    await _card(world, "msg_shop03", _empty_fields())
    agent = make_agent(world)

    response = await run(agent, number_of_batches=1)

    (first,) = agent.firsts
    assert [c["email_id"] for c in numbered(first)] == ["msg_rome01"]
    assert "msg_shop03" not in first and "offers" not in first
    assert response.result["batches_processed"] == 1
    assert all(m is not None for m in await marks(world, "msg_rome01", "msg_shop03"))


async def test_a_pass_of_empty_cards_only_makes_no_model_call_and_marks_them(world):
    await world.emails.save_batch([email("msg_shop03", OLD), email("msg_shop04", OLD + timedelta(hours=1))])
    await _card(world, "msg_shop03", _empty_fields())
    await _card(world, "msg_shop04", _empty_fields())
    prompts = RecordingPrompts()
    agent = make_agent(world, prompts=prompts)

    response = await run(agent, number_of_batches=2)

    assert agent.firsts == [] and prompts.invalidated == []
    assert response.result == {"batches_processed": 0, "facts_affected": 0}
    assert all(m is not None for m in await marks(world, "msg_shop03", "msg_shop04"))


async def test_abandoned_gone_stale_and_unreadable_mail_go_as_their_headers(world):
    policy = dict(subject="Your policy MP-2026-118204", from_address="Example Mutual Insurance <policy@mutual.example>",
                  attachments=["policy_MP-2026-118204.pdf"])
    saved = [email("msg_policy05", OLD, **policy), email("msg_gone06", OLD + timedelta(hours=1)),
             email("msg_stale07", OLD + timedelta(hours=2)), email("msg_broken08", OLD + timedelta(hours=3)),
             email("msg_plain09", OLD + timedelta(hours=4))]
    await world.emails.save_batch(saved)
    await _card(world, "msg_policy05", _abandoned_fields())
    await _card(world, "msg_gone06", gone_fields())
    await _card(world, "msg_stale07", pending_fields(reason="outage", failures=0, model=""))
    await _card(world, "msg_broken08", booking_fields())
    world.db.collection(FACTS).docs[f"{USER}_msg_broken08"]["card"] = {"not": "a card"}
    agent = make_agent(world)

    await run(agent, number_of_batches=1)

    (first,) = agent.firsts
    assert first.startswith(EMAIL_CANDIDATES_HEADER + "\n1. {")
    assert numbered(first) == [header_candidate(e, LISBON) for e in saved]
    assert numbered(first)[0]["attachments"] == [{"name": "policy_MP-2026-118204.pdf", "read": False}]
    assert all(m is not None for m in await marks(world, *[e.email_id for e in saved]))


async def test_an_old_record_with_a_stored_fact_goes_as_a_header_without_it(world):
    await world.emails.save_batch([email("msg_school02", OLD, subject="Parents' meeting",
                                         from_address="Example School <office@school.example>")])
    world.db.collection(FACTS).docs[f"{USER}_msg_school02"].update(
        text="User was invited to the parents' meeting.", tags=["school"], category="personal",
        metadata={"snippet": "The parents' meeting is on 3 March 2026 at 18:00 in the school hall.", "labels": ["INBOX"]})
    agent = make_agent(world)

    await run(agent, number_of_batches=1)

    (candidate,) = numbered(agent.firsts[0])
    assert set(candidate) == {"email_id", "date", "from", "subject"}
    assert candidate["subject"] == "Parents' meeting" and candidate["date"] == "2025-06-02T10:30:00+01:00"
    assert "invited" not in agent.firsts[0] and "school hall" not in agent.firsts[0]


async def test_without_the_card_reads_triage_is_skipped(world):
    await world.emails.save_batch([email("msg_school02", OLD)])
    prompts = RecordingPrompts()
    agent = make_agent(world, batches=False, prompts=prompts)

    response = await run(agent, number_of_batches=1)

    assert response.result == {"batches_processed": 0, "facts_affected": 0, "message": "No email repo"}
    assert agent.firsts == [] and prompts.built == []
    assert await marks(world, "msg_school02") == [None]


async def test_the_pass_line_counts_each_form(world, caplog):
    await world.emails.save_batch([email(e, OLD + timedelta(hours=i)) for i, e in enumerate(
        ["msg_rome01", "msg_shop03", "msg_policy05", "msg_gone06", "msg_stale07", "msg_broken08"])])
    await _card(world, "msg_rome01", booking_fields())
    await _card(world, "msg_shop03", _empty_fields())
    await _card(world, "msg_policy05", _abandoned_fields())
    await _card(world, "msg_gone06", gone_fields())
    await _card(world, "msg_broken08", booking_fields())
    world.db.collection(FACTS).docs[f"{USER}_msg_broken08"]["card"] = {"not": "a card"}
    agent = make_agent(world)

    with caplog.at_level(logging.INFO):
        await run(agent, number_of_batches=1)

    (line,) = [r.getMessage() for r in caplog.records if "Email cards pass 1: cards=" in r.getMessage()]
    assert line.endswith("Email cards pass 1: cards=1 headers=4 skipped=1 (abandoned=1 gone=1 stale=1 unreadable=1) "
                         "withheld=0")
