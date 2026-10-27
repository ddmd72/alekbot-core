"""`FactReviewService.run` with quote translations: the pass is told the fixed language, its translations are stored
right after it, and the question, its history pair and the digest show the fact in the message language (English as the
fallback). The scripted agent of the T8b tests, extended here to return translations like the real pass does."""
import logging
from dataclasses import replace
from typing import Dict

import pytest

from src.domain.agent import AgentResponse
from src.domain.entities import FactState
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest, report_translations
from src.domain.fact_review_result import decode_review_result, encode_review_result
from src.domain.language import LanguageCode
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import FakeLanguageService, ReviewWorld
from tests.unit.services import test_fact_review_selection as selection
from tests.unit.services.test_fact_review_digest import closed, due_world
from tests.unit.services.test_fact_review_selection import SESSION, USER, Rig, ask_all, asked, current, stale

WEIGHT = "Alex keeps a weight log"
WEIGHT_UK = "Алекс веде журнал ваги"
TENNIS = "Alex plays tennis on Sundays"
TENNIS_UK = "Алекс грає в теніс по неділях"


class TranslatingAgent(selection.ScriptedAgent):
    """The scripted agent that also returns `by_text[fact text]` for each batch fact, as the final report's
    `translations` read through `report_translations` (the fixed language of the task, else the reported one)."""

    def __init__(self, world: ReviewWorld) -> None:
        super().__init__(world)
        self.by_text: Dict[str, str] = {}

    async def route_message(self, message) -> AgentResponse:
        response = await super().route_message(message)
        task = self.tasks[-1] if self.tasks else None
        if task is None or response.result is None:
            return response
        raw = {i.fact.id: self.by_text[i.fact.text] for i in task.items if i.fact.text in self.by_text}
        language = self.language if task.mirror else task.language
        kept, _ = report_translations(raw, language, {i.fact.id: i.fact.text for i in task.items})
        result = replace(decode_review_result(response.result), translations=kept)
        return AgentResponse.success(message.task_id, message.recipient, encode_review_result(result))


@pytest.fixture
def uk_rig(monkeypatch):
    """A world whose owner has a Slack channel and a fixed Ukrainian setting, and a rig with the translating agent."""
    async def build(fixed: LanguageCode = LanguageCode.UK, **state):
        monkeypatch.setattr(selection, "ScriptedAgent", TranslatingAgent)
        world = ReviewWorld()
        await world.notification_state.save_primary(USER, "slack", "D1")
        await world.seed_state(account_id=ACC, **state)
        world.language.preferences[USER] = (fixed, False)
        rig = Rig(world)
        rig.agent.by_text = {WEIGHT: WEIGHT_UK, TENNIS: TENNIS_UK}
        return world, rig
    return build


def posted_quotes(world):
    return [message.quote for _, message in world.interactive.posted]


async def test_a_fact_asked_in_the_pass_is_sent_in_its_translation(uk_rig):
    world, rig = await uk_rig()
    await stale(world, WEIGHT, NOW - selection.timedelta(days=9))
    rig.agent.play = ask_all

    assert await rig.run() == {"status": "completed"}

    assert rig.agent.tasks[0].language == "uk" and rig.agent.tasks[0].mirror is False
    assert posted_quotes(world) == [WEIGHT_UK]
    model = (await world.session_store.messages(SESSION))[1].parts[0].text
    assert f"«{WEIGHT_UK}»" in model and WEIGHT not in model
    [question] = await world.review_store.questions_for_account(ACC)
    assert question.quoted_text == WEIGHT


async def test_translations_are_stored_before_the_closes(uk_rig, monkeypatch):
    world, rig = await uk_rig()
    fact = await current(world, WEIGHT)
    await asked(world, "q1", fact, reply_text="yes, still", reply_at=NOW - selection.timedelta(hours=1))
    await world.seed_state(account_id=ACC, current_question_id="q1", sent_on=NOW.date(), sent=1)

    async def play(task, scope):
        basis = {"kind": "reply", "question_id": "q1", "quote": "yes, still"}
        result = await scope.update_fact({"fact_id": fact, "updates": selection.REVIVE, "basis": basis})
        assert result["status"] == "updated", result
    rig.agent.play = play

    async def broken(*args, **kwargs):
        raise RuntimeError("store down")
    monkeypatch.setattr(world.review_store, "close_and_release", broken)

    try:
        await rig.run()
    except Exception:
        pass    # the close raised; what matters is what was kept before it

    docs = [d for d in world.message_docs.values() if d.get("kind") == "quote"]
    assert [(d["language"], d["text"]) for d in docs] == [("uk", WEIGHT_UK)]


async def test_a_question_queued_before_translations_is_sent_in_english(uk_rig):
    world, _ = await uk_rig()
    fact = await current(world, WEIGHT)
    await world.seed_question(selection.QuestionStatus.QUEUED, question_id="q1", account_id=ACC, user_id=USER,
                              fact_id=fact, quoted_text=WEIGHT)

    await Rig(world).run()

    assert posted_quotes(world) == [WEIGHT]


async def test_a_mirror_task_carries_no_fixed_language(uk_rig):
    world, rig = await uk_rig()
    world.language.preferences[USER] = (LanguageCode.UK, True)
    await stale(world, WEIGHT, NOW - selection.timedelta(days=9))
    rig.agent.language = "uk"

    await rig.run()

    (task,) = rig.agent.tasks
    assert task.mirror is True and task.language is None


async def test_a_failed_language_read_sends_no_language_and_the_run_completes(uk_rig, caplog):
    world, rig = await uk_rig()

    class OnceThenBroken(FakeLanguageService):
        def __init__(self, inner):
            super().__init__()
            self.preferences = inner.preferences
            self.calls = 0

        async def get_preference(self, user_id):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("user store unavailable")
            return await super().get_preference(user_id)

    world.language = OnceThenBroken(world.language)
    rig = Rig(world)
    rig.agent.by_text = {WEIGHT: WEIGHT_UK}
    await stale(world, WEIGHT, NOW - selection.timedelta(days=9))

    with caplog.at_level(logging.WARNING):
        assert await rig.run() == {"status": "completed"}

    assert rig.agent.tasks[0].language is None
    assert any("translates nothing" in r.getMessage() for r in caplog.records)


async def test_a_failed_translation_write_leaves_the_run_completed(uk_rig, monkeypatch, caplog):
    world, rig = await uk_rig()
    await stale(world, WEIGHT, NOW - selection.timedelta(days=9))
    rig.agent.play = ask_all

    async def broken(*args, **kwargs):
        raise RuntimeError("store down")
    monkeypatch.setattr(world.review_store, "set_quote_translations", broken)

    with caplog.at_level(logging.WARNING):
        assert await rig.run() == {"status": "completed"}

    warned = " ".join(r.getMessage() for r in caplog.records if "not stored" in r.getMessage())
    assert "1 quote translations" in warned and "RuntimeError" in warned and WEIGHT_UK not in warned
    assert posted_quotes(world) == [WEIGHT]


async def test_the_digest_quotes_each_fact_in_its_translation_and_the_rest_as_stored():
    world = await due_world()
    world.language.preferences[USER] = (LanguageCode.UK, False)
    await closed(world, WEIGHT, "review", 1)
    await closed(world, "Alex orders a cookbook", "owner", 2, FactState.INVALIDATED)
    await world.review_store.set_quote_translations(
        ACC, [QuoteTranslation("uk", quote_digest(WEIGHT), WEIGHT_UK)], NOW,
    )

    await Rig(world).run()

    [text] = [t for _, _, t in world.channels.sent]
    assert f"«{WEIGHT_UK}»" in text and "«Alex orders a cookbook»" in text and WEIGHT not in text
