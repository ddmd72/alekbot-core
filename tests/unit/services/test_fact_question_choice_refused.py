"""A button click the owner's answer could not be applied for (Facts S4 · T8d2, owner decision O3, plan §3.20): the fact
changed meanwhile or another writer won. The message shows the same `MEMORY_FACT_CHANGED` line a digit gets and keeps its
buttons; the question stays open and nothing is written. A refused write (`not_applied`) shows nothing.

The real `FactAnswerService` on the review world, behind the real `InteractiveChoiceService` and the real Slack
adapter over a mocked `slack_sdk` client, so what the click leaves on the Slack message is read from `chat_update`."""
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.binding_codec import decode
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user import UserProfile
from src.services.interactive_choice_service import InteractiveChoiceService
from tests.support.fact_kit import ACC
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_question_choice import (
    SLACK_USER, USER, ScriptedWrites, choice_of, day_row, ref_of, service, snapshot, stale_fact, stored, question_on,
    texts_of,
)
from tests.unit.services.test_interactive_choice_service import FakeIam

OPTIONS = ["still_true", "ended", "never", "dont_know"]


def slack_client() -> Any:
    client = MagicMock()
    client.chat_update = AsyncMock(return_value={"ok": True})
    return client


def pipeline(world: ReviewWorld, client: Any, **service_args: Any) -> InteractiveChoiceService:
    iam = FakeIam({("slack", SLACK_USER): UserProfile(user_id=USER, account_id=ACC)})
    return InteractiveChoiceService(
        iam, {"fact_question": service(world, **service_args)}, SlackInteractiveMessageAdapter(client))


async def click(world: ReviewWorld, client: Any, option: str, **service_args: Any) -> dict:
    return await pipeline(world, client, **service_args).handle(choice_of(option, ref_of(world)))


def note_of(world: ReviewWorld, language: LanguageCode = LanguageCode.EN) -> str:
    return world.localization.get_ui_string(language, UIMessage.MEMORY_FACT_CHANGED)


def assert_buttons_kept_with(client: Any, note: str, world: ReviewWorld) -> None:
    client.chat_update.assert_awaited_once()
    kwargs = client.chat_update.call_args.kwargs
    ref = ref_of(world)
    assert (kwargs["channel"], kwargs["ts"]) == (ref.channel_id, ref.message_id)
    blocks = kwargs["blocks"]
    assert [b["type"] for b in blocks] == ["section", "section", "context", "context", "actions"]
    assert blocks[2]["elements"][0]["text"] == note                                  # the note under the quote
    actions = blocks[-1]
    assert decode(actions["block_id"]).id == "q1" and decode(actions["block_id"]).kind == "fact_question"
    assert [e["action_id"] for e in actions["elements"]] == OPTIONS
    assert note in kwargs["text"]


class TestRefusedClick:
    async def test_a_fact_that_changed_shows_the_line_and_keeps_the_buttons_and_the_question_open(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        world.docs[fact]["state"] = "archived"                      # closed meanwhile: `changed`
        before = snapshot(world)
        client = slack_client()

        assert await click(world, client, "still_true") == {"status": "shown"}

        assert_buttons_kept_with(client, note_of(world), world)
        assert snapshot(world) == before and stored(world)["status"] == "asked"      # nothing written, still open

    @pytest.mark.parametrize("option", OPTIONS[:3])
    async def test_a_write_that_conflicted_shows_the_same_line_and_keeps_the_buttons(self, option):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        writes = ScriptedWrites(lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.CONFLICT, "changed meanwhile"))
        client = slack_client()

        assert await click(world, client, option, fact_updates=writes) == {"status": "shown"}

        assert_buttons_kept_with(client, note_of(world), world)
        assert stored(world)["status"] == "asked" and world.docs[fact]["state"] == "stale"

    async def test_the_line_is_the_one_a_digit_gets_in_the_owners_language(self):
        world = ReviewWorld()
        world.language.preferences[USER] = (LanguageCode.FR, False)
        fact = await stale_fact(world)
        await question_on(world, fact)
        world.docs[fact]["state"] = "archived"
        client = slack_client()

        await click(world, client, "never")

        french = note_of(world, LanguageCode.FR)
        assert french != note_of(world)
        kwargs = client.chat_update.call_args.kwargs
        assert kwargs["blocks"][2]["elements"][0]["text"] == french
        [question] = await world.review_store.get_questions(["q1"])
        labels = await texts_of(world).labels_for(USER, ACC)
        assert labels[UIMessage.MEMORY_OPTION_NEVER.value] != "Never"                  # the buttons are French too
        assert [e["text"]["text"] for e in kwargs["blocks"][-1]["elements"]] == [
            o.label for o in fact_question_message(question, labels).options]

    async def test_a_second_click_after_the_refusal_is_applied_when_the_fact_is_there_again(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        outcomes = iter([UpdateRefusal.CONFLICT, None])

        class Flaky(ScriptedWrites):
            async def apply_fact_update(self, fact_id, update, account_id, **kwargs):
                refusal = next(outcomes)
                if refusal is None:
                    return await world.store.adapter.apply_fact_update(fact_id, update, account_id, **kwargs)
                return FactUpdateOutcome.refused(fact_id, refusal, "changed meanwhile")

        writes = Flaky(lambda fid: None)
        first = slack_client()
        await click(world, first, "never", fact_updates=writes)
        assert stored(world)["status"] == "asked"
        second = slack_client()

        await click(world, second, "never", fact_updates=writes)

        assert stored(world)["status"] == "closed" and world.docs[fact]["state"] == "invalidated"
        assert [b["type"] for b in second.chat_update.call_args.kwargs["blocks"]] == ["section", "section", "context"]

    async def test_a_write_the_rules_refused_shows_nothing_and_changes_nothing(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        writes = ScriptedWrites(lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.INVALID, "refused by the rules"))
        client = slack_client()

        assert await click(world, client, "never", fact_updates=writes) == {"status": "ignored"}

        client.chat_update.assert_not_awaited()
        assert stored(world)["status"] == "asked" and (await day_row(world)).answers_not_applied == 1

    async def test_the_handler_returns_a_note_with_the_options_kept(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        world.docs[fact]["state"] = "archived"

        outcome = await service(world).handle(choice_of("ended", ref_of(world)), USER, ACC)

        assert outcome is not None and outcome.keep_options is True and outcome.chosen_option_id is None
        assert outcome.note == note_of(world)
        assert [o.id for o in outcome.message.options] == OPTIONS

