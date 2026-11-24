"""ExternalTaskSendChoice: a press of the Slack «Send» button on a held task's command message ends in the command's own
`confirm(user_id, code)` and shows the command's own answer in place of the button (EXTERNAL_TASK_QUEUE_RFC §5.3).

The real `ExternalTaskService` over the fakes of `tests/support/external_task_world.py` (the flag and the lock are
written into the stored user and read fresh), the real file localization and the review world's language fake. The
choice is the one the Slack route builds from a press of the posted message. Alex Doe is `usr-1`; `usr-2` is another
user of the same bot. Assertions are on the stored task and on the outcome the message is told to show."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.external_task import HOLD_S, ExternalTaskStatus
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_jobs import EXTERNAL_TASK_TIMER
from src.domain.external_task_receipt import EXTERNAL_TASK_SEND_KIND, send_button_message
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import InteractiveChoice
from src.domain.language import LanguageCode
from src.domain.messaging import MessageRef
from src.domain.ui_messages import UIMessage
from src.domain.user import InactiveReason
from src.locales import en, uk
from src.services.external_task_send_choice import ExternalTaskSendChoice
from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from tests.support.external_task_world import (
    ACCOUNT_ID, DIGEST_KEY, ORIGIN, TEXT, TITLE, USER_ID, USERS, ExternalTaskWorld,
)
from tests.support.fact_review_world import FakeLanguageService

S = ExternalTaskStatus
REF = MessageRef("slack", "D1", "1761295900.000200")
PRESSED_AT = datetime(2026, 11, 23, 9, 1, tzinfo=timezone.utc)


def _en(message: UIMessage, **fmt) -> str:
    template = en.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


def _choice(task_id: str, code: str) -> InteractiveChoice:
    return InteractiveChoice(binding=InteractiveBinding(kind=EXTERNAL_TASK_SEND_KIND, id=task_id), option_id=code,
                             platform="slack", platform_user_id="U0OWNER", message_ref=REF, at=PRESSED_AT)


def _handler(service, language: FakeLanguageService | None = None) -> ExternalTaskSendChoice:
    if language is None:
        language = FakeLanguageService()
        language.preferences[USER_ID] = (LanguageCode.EN, False)
    return ExternalTaskSendChoice(service, language, FileLocalizationAdapter())


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


async def _held(world):
    return await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT)


def test_the_handler_answers_the_send_buttons_binding_kind():
    assert _handler(object()).kind == EXTERNAL_TASK_SEND_KIND == "external_task_send"


async def test_the_owners_press_queues_the_held_task_and_shows_the_commands_sent_line_without_the_button(world):
    created = await _held(world)

    outcome = await _handler(world.service).handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)

    assert world.tasks.tasks[created.task_id].status is S.QUEUED
    assert outcome is not None
    assert outcome.note == _en(UIMessage.EXTERNAL_TASK_SENT, title=TITLE, worker="research")
    assert outcome.keep_options is False and outcome.chosen_option_id is None
    assert outcome.message == send_button_message(created.task_id, created.code, "Send")


async def test_the_press_of_another_user_releases_nothing_and_shows_nothing(world):
    created = await _held(world)

    outcome = await _handler(world.service).handle(_choice(created.task_id, created.code), "usr-2", "acc-2")

    assert outcome is None
    assert world.tasks.tasks[created.task_id].status is S.HELD
    assert [b["kind"] for b in world.jobs(EXTERNAL_TASK_TIMER)] == ["hold"]


async def test_another_users_press_shows_nothing_even_when_their_queue_is_off(world):
    created = await _held(world)
    await world.flag("usr-2", False)

    assert await _handler(world.service).handle(_choice(created.task_id, created.code), "usr-2", "acc-2") is None
    assert world.tasks.tasks[created.task_id].status is S.HELD


async def test_a_second_press_gets_the_commands_not_found_line_and_never_a_second_release(world):
    created = await _held(world)
    handler = _handler(world.service)
    await handler.handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)
    released = world.tasks.tasks[created.task_id]

    outcome = await handler.handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)

    assert outcome is not None and outcome.note == _en(UIMessage.EXTERNAL_TASK_NOT_FOUND)
    assert outcome.keep_options is False
    assert world.tasks.tasks[created.task_id] == released
    assert [b["kind"] for b in world.jobs(EXTERNAL_TASK_TIMER)] == ["hold", "deadline"]


async def test_a_press_after_the_command_sent_the_task_gets_not_found(world):
    created = await _held(world)
    await world.service.confirm(USER_ID, created.code)

    outcome = await _handler(world.service).handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)

    assert outcome is not None and outcome.note == _en(UIMessage.EXTERNAL_TASK_NOT_FOUND)
    assert outcome.keep_options is False


async def test_a_press_after_the_hold_lapsed_gets_not_found_and_sends_nothing(world):
    created = await _held(world)
    later = ExternalTaskService(
        world.tasks, world.workers, world.queue, world.users, InactiveUserCheck(world.users), digest_key=DIGEST_KEY,
        clock=lambda: datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc) + timedelta(seconds=HOLD_S))

    outcome = await _handler(later).handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)

    assert outcome is not None and outcome.note == _en(UIMessage.EXTERNAL_TASK_NOT_FOUND)
    assert world.tasks.tasks[created.task_id].status is S.HELD


async def test_a_switched_off_queue_gets_unavailable_and_keeps_the_button_for_a_later_press(world):
    created = await _held(world)
    handler = _handler(world.service)
    await world.flag(USER_ID, False)

    refused = await handler.handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)

    assert refused is not None and refused.note == _en(UIMessage.EXTERNAL_TASK_UNAVAILABLE)
    assert refused.keep_options is True
    assert world.tasks.tasks[created.task_id].status is S.HELD
    await world.flag(USER_ID, True)
    sent = await handler.handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)
    assert sent is not None and sent.note == _en(UIMessage.EXTERNAL_TASK_SENT, title=TITLE, worker="research")


async def test_an_account_locked_after_the_press_was_authorised_gets_unavailable_and_nothing_is_sent(world):
    created = await _held(world)
    await world.db.collection(USERS).document(USER_ID).update(
        {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})

    outcome = await _handler(world.service).handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)

    assert outcome is not None and outcome.note == _en(UIMessage.EXTERNAL_TASK_UNAVAILABLE)
    assert world.tasks.tasks[created.task_id].status is S.HELD


async def test_an_unexpected_error_of_the_release_is_logged_and_answered_unavailable_with_the_button_kept(world, caplog):
    created = await _held(world)

    async def down(user_id, code):
        raise RuntimeError("firestore down")
    world.service.confirm = down

    with caplog.at_level(logging.ERROR):
        outcome = await _handler(world.service).handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)

    assert outcome is not None and outcome.note == _en(UIMessage.EXTERNAL_TASK_UNAVAILABLE)
    assert outcome.keep_options is True
    assert any("send button" in r.getMessage() and "RuntimeError" in r.getMessage() for r in caplog.records)
    assert all(created.code not in r.getMessage() for r in caplog.records)


async def test_a_store_error_of_the_owner_check_raises_before_anything_is_written(world):
    created = await _held(world)

    async def down(user_id, task_id):
        raise RuntimeError("firestore down")
    world.tasks.get = down

    with pytest.raises(RuntimeError, match="firestore down"):
        await _handler(world.service).handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)
    assert world.tasks.tasks[created.task_id].status is S.HELD


async def test_the_answer_and_the_button_are_in_the_owners_ui_language(world):
    created = await _held(world)
    language = FakeLanguageService()
    language.preferences[USER_ID] = (LanguageCode.UK, False)

    outcome = await _handler(world.service, language).handle(_choice(created.task_id, created.code), USER_ID,
                                                             ACCOUNT_ID)

    assert outcome is not None
    assert outcome.note == uk.UI_STRINGS[UIMessage.EXTERNAL_TASK_SENT.value].format(title=TITLE, worker="research")
    assert [o.label for o in outcome.message.options] == [uk.UI_STRINGS[UIMessage.EXTERNAL_TASK_SEND_BUTTON.value]]


async def test_no_log_line_holds_the_code(world, caplog):
    created = await _held(world)
    handler = _handler(world.service)

    with caplog.at_level(logging.DEBUG):
        await handler.handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)
        await handler.handle(_choice(created.task_id, created.code), USER_ID, ACCOUNT_ID)
        await handler.handle(_choice(created.task_id, created.code), "usr-2", "acc-2")

    assert caplog.records and all(created.code not in r.getMessage() for r in caplog.records)
