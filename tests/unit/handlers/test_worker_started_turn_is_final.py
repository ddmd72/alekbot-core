"""A background turn that started is final (P15-D1): a reminder fire or a daily review is not retried once its turn
started, and a reminder fire that ends final with nothing sent tells the owner once that it did not run.

On the redelivery world (`tests/support/redelivery_world.py`): the real `WorkerHandler`, claim store, note store and
`UserNotificationService` (with the real localization, `languages=`), a scripted agent turn and recording channels.
`deliver_with_retry` redelivers only when Cloud Tasks would (a raise, `>= 500`, `429`), so a list of one outcome is a
fire that was not retried. The turn cap is what `SmartResponseAgent` answers when its tool loop ends without an answer
(`TURN_CAP_ERROR`).
"""
import logging
from datetime import timedelta
from typing import Any

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.language import LanguageCode
from src.domain.messaging import RichContent
from src.domain.notify_result import EMPTY_ANSWER_ERROR, TURN_CAP_ERROR
from src.domain.ui_messages import UIMessage
from tests.support.fact_review_world import FakeLanguageService
from tests.support.redelivery_world import (
    ACCOUNT, CHANNEL, T0, USER, RedeliveryWorld, ScriptedReview, deliver_with_retry, failed, ok, timed_out,
)

TEXT = "Time to call dad"
TABLE = RichContent(content_type="table", data={"headers": ["Activity", "Time"], "rows": [{"cells": ["Yoga", "Morning"]}]},
                    fallback_text="Yoga: Morning")
EMAILS = [{"id": "m1", "from": "school@example.com", "subject": "Parents' meeting moved to Saturday", "body": "See you."}]
NAME = "call dad"   # the text `RedeliveryWorld.seed_note` stores


def _notice(lang: LanguageCode) -> str:
    return FileLocalizationAdapter().get_ui_string(lang, UIMessage.REMINDER_NOT_RUN).format(name=NAME)


async def _world(lang: LanguageCode = LanguageCode.EN) -> RedeliveryWorld:
    languages = FakeLanguageService()
    languages.preferences[USER] = (lang, False)
    world = RedeliveryWorld(languages=languages)
    await world.set_primary_channel()
    return world


async def _fire(world: RedeliveryWorld, monkeypatch: Any, recurrence: str = "") -> tuple[str, dict]:
    note_id = await world.seed_note(monkeypatch, due=T0, recurrence=recurrence or None,
                                    last_fired=T0 + timedelta(seconds=30), created_at=T0 - timedelta(days=1))
    return note_id, world.payload("execute_reminder", note_id=note_id, user_id=USER, due_at=T0.isoformat())


class TestTurnCap:
    async def test_a_one_time_reminder_at_the_turn_cap_is_acked_once_and_the_owner_gets_one_notice(self, monkeypatch):
        world = await _world()
        world.router.always(failed(TURN_CAP_ERROR))
        note_id, payload = await _fire(world, monkeypatch)

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "turn_cap_final"}, 200)]
        assert len(world.router.messages) == 1
        assert [(p.channel_id, p.kind, p.value) for p in world.posts] == [(CHANNEL, "literal", _notice(LanguageCode.EN))]
        assert note_id not in world.note_docs
        assert [d["state"] for d in world.claim_docs] == ["done"]

    async def test_a_recurring_reminder_at_the_turn_cap_keeps_its_note_stamped_and_the_notice_is_in_the_ui_language(
            self, monkeypatch):
        world = await _world(LanguageCode.UK)
        world.router.always(failed(TURN_CAP_ERROR))
        note_id, payload = await _fire(world, monkeypatch, recurrence="FREQ=DAILY")

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "turn_cap_final"}, 200)]
        assert world.texts == [_notice(LanguageCode.UK)]
        assert world.note_docs[note_id]["last_delivered_due"] == T0

    async def test_a_turn_cap_whose_notice_fails_to_post_is_still_final(self, monkeypatch):
        world = await _world()
        world.router.always(failed(TURN_CAP_ERROR))
        world.channels.fail_post(1)                         # the notice is the only post
        _, payload = await _fire(world, monkeypatch)

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "turn_cap_final"}, 200)]
        assert len(world.router.messages) == 1 and world.posts == []

    async def test_a_second_delivery_of_a_turn_cap_fire_neither_runs_nor_notifies_again(self, monkeypatch):
        """Two task names for one fire (a lost ack): the stamp written before the notice answers the second."""
        world = await _world()
        world.router.always(failed(TURN_CAP_ERROR))
        _, payload = await _fire(world, monkeypatch, recurrence="FREQ=DAILY")

        await deliver_with_retry(world.handler, payload, "t-1")
        second = await deliver_with_retry(world.handler, payload, "t-2")

        assert second == [({"status": "already_delivered"}, 200)]
        assert len(world.router.messages) == 1 and len(world.texts) == 1


class TestAfterTheStamp:
    """Nothing after the stamp is retryable: a retry would answer `already_delivered` and never send the notice."""

    async def test_a_delete_that_raises_still_sends_the_notice_once_and_answers_2xx(self, monkeypatch, caplog):
        world = await _world()
        world.router.always(failed("provider down"))
        note_id, payload = await _fire(world, monkeypatch)

        async def broken_delete(**kwargs: Any) -> None:
            raise RuntimeError("delete lost")

        monkeypatch.setattr(world.notes, "delete_note", broken_delete)
        with caplog.at_level(logging.ERROR):
            outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "failed_final"}, 200)]
        assert world.texts == [_notice(LanguageCode.EN)] and len(world.router.messages) == 1
        assert world.note_docs[note_id]["last_delivered_due"] == T0
        assert any(r.levelno == logging.ERROR and "delete after the stamp failed: delete lost" in r.getMessage()
                   for r in caplog.records)

    async def test_a_notice_that_raises_answers_2xx_logs_an_error_and_still_deletes_the_note(self, monkeypatch, caplog):
        world = await _world()
        world.router.always(failed(TURN_CAP_ERROR))
        note_id, payload = await _fire(world, monkeypatch)

        resolve = world.notifier.resolve_channel
        calls = []

        async def broken_after_the_turn(*args: Any, **kwargs: Any) -> Any:
            calls.append(args)
            if len(calls) > 1:                              # the turn's own resolve works; the notice's raises
                raise RuntimeError("state store down")
            return await resolve(*args, **kwargs)

        monkeypatch.setattr(world.notifier, "resolve_channel", broken_after_the_turn)
        with caplog.at_level(logging.ERROR):
            outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "turn_cap_final"}, 200)]
        assert world.posts == [] and len(world.router.messages) == 1
        assert note_id not in world.note_docs
        assert any(r.levelno == logging.ERROR and "notice failed: state store down" in r.getMessage()
                   for r in caplog.records)


class TestStartedTurnIsFinal:
    async def test_a_failed_turn_is_acked_without_a_retry_and_the_owner_gets_one_notice(self, monkeypatch):
        world = await _world()
        world.router.always(failed("provider down"))
        note_id, payload = await _fire(world, monkeypatch, recurrence="FREQ=DAILY")

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "failed_final"}, 200)]
        assert len(world.router.messages) == 1
        assert [(p.channel_id, p.kind, p.value) for p in world.posts] == [(CHANNEL, "literal", _notice(LanguageCode.EN))]
        assert world.note_docs[note_id]["last_delivered_due"] == T0

    async def test_a_timed_out_turn_is_final_and_the_owner_gets_one_notice(self, monkeypatch):
        world = await _world(LanguageCode.FR)
        world.router.always(timed_out())
        note_id, payload = await _fire(world, monkeypatch)

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "failed_final"}, 200)]
        assert len(world.router.messages) == 1
        assert world.texts == [_notice(LanguageCode.FR)]
        assert note_id not in world.note_docs

    async def test_an_empty_answer_is_final_and_the_owner_gets_one_notice(self, monkeypatch):
        world = await _world()
        world.router.always(failed(EMPTY_ANSWER_ERROR))
        _, payload = await _fire(world, monkeypatch)

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "empty_answer_final"}, 200)]
        assert world.texts == [_notice(LanguageCode.EN)]

    async def test_a_second_delivery_of_a_failed_fire_neither_runs_nor_notifies_again(self, monkeypatch):
        """Two task names for one fire (a lost ack): the stamp written before the notice answers the second."""
        world = await _world()
        world.router.always(failed("provider down"))
        _, payload = await _fire(world, monkeypatch, recurrence="FREQ=DAILY")

        await deliver_with_retry(world.handler, payload, "t-1")
        second = await deliver_with_retry(world.handler, payload, "t-2")

        assert second == [({"status": "already_delivered"}, 200)]
        assert len(world.router.messages) == 1 and world.texts == [_notice(LanguageCode.EN)]

    async def test_a_text_whose_post_raised_is_not_posted_again_and_gets_no_notice(self, monkeypatch):
        """The post may have left before it raised (`NotifyResult.sent`): a re-run could double it, and the owner may
        have the answer, so no "did not run" notice."""
        world = await _world()
        world.router.always(ok(TEXT))
        world.channels.fail_post(1)
        _, payload = await _fire(world, monkeypatch)

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "failed_final"}, 200)]
        assert len(world.router.messages) == 1 and world.posts == []

    async def test_a_text_posted_before_its_rich_part_failed_gets_no_notice(self, monkeypatch):
        world = await _world()
        world.router.always(ok(TEXT, rich=TABLE))
        world.channels.fail_post(2)                         # the text is post 1, the rich part post 2
        _, payload = await _fire(world, monkeypatch)

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "failed_final"}, 200)]
        assert world.texts == [TEXT]

    async def test_a_delivered_reminder_is_ok_and_posts_no_notice(self, monkeypatch):
        """Control: a delivered reminder needs no notice."""
        world = await _world()
        world.router.always(ok(TEXT))
        _, payload = await _fire(world, monkeypatch)

        assert await deliver_with_retry(world.handler, payload, "t-1") == [({"status": "ok"}, 200)]
        assert world.texts == [TEXT]

    async def test_a_channel_adapter_that_is_missing_is_still_retried_and_the_retry_posts_once(self, monkeypatch):
        """Control: `notify` stopped before the agent (no adapter for the platform), so the 500 and its retry are right."""
        world = await _world()
        world.router.always(ok(TEXT))
        world.channels.unconfigured.add("slack")
        _, payload = await _fire(world, monkeypatch)

        first = await deliver_with_retry(world.handler, payload, "t-1", attempts=1)
        world.channels.unconfigured.clear()
        second = await deliver_with_retry(world.handler, payload, "t-1")

        assert [o[1] for o in first] == [500]                # type: ignore[index]
        assert first[0][0]["error"] == "channel_factory_returned_none:slack"   # type: ignore[index]
        assert second == [({"status": "ok"}, 200)]
        assert world.texts == [TEXT] and len(world.router.messages) == 1


class TestDailyReviewStartedTurnIsFinal:
    async def test_a_failed_review_turn_is_acked_without_a_retry(self):
        world = RedeliveryWorld(email_review=ScriptedReview(EMAILS))
        await world.set_primary_channel()
        world.router.always(failed("provider down"))
        payload = world.payload("daily_email_review", user_id=USER, account_id=ACCOUNT)

        outcomes = await deliver_with_retry(world.handler, payload, "t-1")

        assert outcomes == [({"status": "failed_final", "emails": 1}, 200)]
        assert len(world.router.messages) == 1 and world.posts == []
