"""`NotifyResult.turn_started` says whether `notify()` handed the alert to the agent, so a Cloud-Task-backed caller
answers 5xx (a retry) only when nothing ran (P15-D1, `is_retryable_failure`); `notify_ui_notice` posts one fixed UI
string in the owner's UI language. On the redelivery world's real `UserNotificationService`, recording channels and
scripted router."""
from typing import Optional

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import AgentStatus
from src.domain.language import LanguageCode
from src.domain.notification_kind import NotificationKind
from src.domain.notify_result import (
    EMPTY_ANSWER_ERROR, TURN_CAP_ERROR, NotifyResult, is_retryable_failure, is_turn_cap,
)
from src.domain.ui_messages import UIMessage
from tests.support.fact_review_world import FakeLanguageService
from tests.support.redelivery_world import ACCOUNT, CHANNEL, USER, RedeliveryWorld, Turn, failed, ok

NAME = "water the plants"


def _world(lang: Optional[LanguageCode] = None) -> RedeliveryWorld:
    if lang is None:
        return RedeliveryWorld()
    languages = FakeLanguageService()
    languages.preferences[USER] = (lang, False)
    return RedeliveryWorld(languages=languages)


async def _notify(world: RedeliveryWorld) -> NotifyResult:
    return await world.notifier.notify(USER, ACCOUNT, "alert", kind=NotificationKind.REMINDER)


class TestTurnStarted:
    async def test_no_channel_never_starts_the_turn(self):
        world = _world()
        world.router.always(ok("hi"))

        result = await _notify(world)

        assert (result.error, result.turn_started) == ("no_channel", False)
        assert world.router.messages == []

    async def test_a_missing_channel_adapter_never_starts_the_turn(self):
        world = _world()
        await world.set_primary_channel()
        world.channels.unconfigured.add("slack")
        world.router.always(ok("hi"))

        result = await _notify(world)

        assert (result.error, result.turn_started) == ("channel_factory_returned_none:slack", False)
        assert world.router.messages == []

    @pytest.mark.parametrize("turn", [ok("hi"), failed("provider down"), failed(TURN_CAP_ERROR)],
                             ids=["delivered", "failed", "turn-cap"])
    async def test_a_turn_handed_to_the_agent_has_started(self, turn: Turn):
        world = _world()
        await world.set_primary_channel()
        world.router.always(turn)

        result = await _notify(world)

        assert result.turn_started is True and len(world.router.messages) == 1

    async def test_a_router_that_raises_has_started_the_turn(self):
        world = _world()
        await world.set_primary_channel()
        world.router.always(Turn(response=ok("hi").response, raises=RuntimeError("router down")))

        result = await _notify(world)

        assert (result.delivered, result.agent_status, result.turn_started) == (False, AgentStatus.FAILED, True)

    async def test_a_send_that_raised_has_started_the_turn_and_sent(self):
        world = _world()
        await world.set_primary_channel()
        world.router.always(ok("hi"))
        world.channels.fail_post(1)

        result = await _notify(world)

        assert (result.delivered, result.sent, result.turn_started) == (False, True, True)


class TestOutcomeRules:
    @pytest.mark.parametrize("result,retry,cap", [
        (NotifyResult(delivered=False, agent_status=AgentStatus.SUCCESS, error="no_channel"), True, False),
        (NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="x", turn_started=True), False, False),
        (NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error=TURN_CAP_ERROR, turn_started=True),
         False, True),
        (NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error=TURN_CAP_ERROR, sent=True,
                      turn_started=True), False, False),
        (NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error=EMPTY_ANSWER_ERROR, turn_started=True),
         False, False),
        (NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True, turn_started=True), False, False),
    ], ids=["no-channel", "failed-turn", "turn-cap", "turn-cap-after-a-send", "empty-answer", "delivered"])
    def test_retry_only_when_nothing_ran_and_turn_cap_only_when_nothing_was_sent(self, result, retry, cap):
        assert (is_retryable_failure(result), is_turn_cap(result)) == (retry, cap)


class TestUiNotice:
    @pytest.mark.parametrize("lang", [LanguageCode.EN, LanguageCode.UK, LanguageCode.FR, LanguageCode.ES])
    async def test_the_notice_is_posted_literally_in_the_ui_language_and_never_runs_the_agent(self, lang):
        world = _world(lang)
        await world.set_primary_channel()

        sent = await world.notifier.notify_ui_notice(USER, ACCOUNT, UIMessage.REMINDER_NOT_RUN_TURN_CAP, name=NAME)

        text = FileLocalizationAdapter().get_ui_string(lang, UIMessage.REMINDER_NOT_RUN_TURN_CAP).format(name=NAME)
        assert sent is True
        assert [(p.channel_id, p.kind, p.value) for p in world.posts] == [(CHANNEL, "literal", text)]
        assert NAME in text and "{" not in text
        assert world.router.messages == []

    async def test_without_localization_nothing_is_posted(self):
        world = _world()
        await world.set_primary_channel()

        assert await world.notifier.notify_ui_notice(USER, ACCOUNT, UIMessage.REMINDER_NOT_RUN_TURN_CAP,
                                                     name=NAME) is False
        assert world.posts == []

    async def test_without_a_channel_nothing_is_posted(self):
        world = _world(LanguageCode.EN)

        assert await world.notifier.notify_ui_notice(USER, ACCOUNT, UIMessage.REMINDER_NOT_RUN_TURN_CAP,
                                                     name=NAME) is False
        assert world.posts == []

    @pytest.mark.parametrize("lang", [LanguageCode.EN, LanguageCode.UK, LanguageCode.FR, LanguageCode.ES])
    def test_the_notice_never_claims_the_system_voice(self, lang):
        text = FileLocalizationAdapter().get_ui_string(lang, UIMessage.REMINDER_NOT_RUN_TURN_CAP)
        assert "[System" not in text and "SYSTEM:" not in text
