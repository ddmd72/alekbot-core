"""Facts S4 · T7b fix round (S1): every read `FactQuestionTexts` makes (the language setting, the review state, the UI
language) is logged, naming the read, and raised when it fails. On `ReviewWorld`, with the language service made to
raise at one read."""
import logging
from typing import List

import pytest

from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.services.fact_question_texts import FactQuestionTexts
from tests.support.fact_review_world import FakeLanguageService, ReviewWorld

USER, ACC = "u1", "acc"


class FailingLanguage(FakeLanguageService):
    """The fake language service, raising at the read named by `fails`."""

    def __init__(self, fails: str) -> None:
        super().__init__()
        self.fails = fails
        self.preferences[USER] = (LanguageCode.UK, True)

    async def get_preference(self, user_id: str):
        if self.fails == "get_preference":
            raise RuntimeError("user store unavailable")
        return await super().get_preference(user_id)

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        if self.fails == "resolve_ui_language":
            raise RuntimeError("account store unavailable")
        return await super().resolve_ui_language(user_id)


def errors(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.parametrize("fails,named,why", [
    ("get_preference", "the language setting", "user store unavailable"),
    ("resolve_ui_language", "the UI language", "account store unavailable"),
])
@pytest.mark.parametrize("call", ["language_for", "labels_for", "text"])
async def test_a_raising_language_service_is_logged_naming_the_read_and_raised(caplog, fails, named, why, call):
    world = ReviewWorld()
    await world.seed_state()
    resolver = FactQuestionTexts(FailingLanguage(fails), world.localization, world.review_store, world.counters,
                                 system_default=LanguageCode.EN, clock=world.clock)
    args = (USER, ACC, UIMessage.MEMORY_ANSWER_NOTED) if call == "text" else (USER, ACC)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match=why):
        await getattr(resolver, call)(*args)

    (found,) = errors(caplog)
    assert named in found and ACC[:8] in found and "RuntimeError" in found and why in found


async def test_a_raising_language_setting_read_in_mirrors_is_logged_and_raised(caplog):
    world = ReviewWorld()
    resolver = FactQuestionTexts(FailingLanguage("get_preference"), world.localization, world.review_store, world.counters,
                                 system_default=LanguageCode.EN, clock=world.clock)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="user store unavailable"):
        await resolver.mirrors(USER)

    (found,) = errors(caplog)
    assert "the language setting" in found and USER[:8] in found and "RuntimeError" in found


async def test_a_raising_state_read_is_named_as_the_review_state(caplog):
    world = ReviewWorld()
    world.language.preferences[USER] = (LanguageCode.UK, True)

    class RaisingStates:
        async def get_state(self, account_id: str):
            raise TimeoutError("deadline")

    resolver = FactQuestionTexts(world.language, world.localization, RaisingStates(), world.counters,
                                 system_default=LanguageCode.EN, clock=world.clock)

    with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError):
        await resolver.language_for(USER, ACC)

    (found,) = errors(caplog)
    assert "the review state" in found and "TimeoutError: deadline" in found
