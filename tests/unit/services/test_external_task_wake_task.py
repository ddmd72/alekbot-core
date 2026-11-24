"""The `external_task_wake` runner (EXTERNAL_TASK_QUEUE_RFC §5.8 "The wake task", §7 wake-up rows).

The world of `test_external_task_wake.py` (the service with the test cipher and a recording webhook) plus the real
UserNotificationService with the real localization and a recording channel. Assertions are on the HTTP answer Cloud
Tasks acts on, the literal post the owner gets and the log lines.
"""
import logging
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.external_task import WAKE_MAX_ATTEMPTS
from src.domain.language import LanguageCode
from src.domain.wake_refusal import WakeRefusal
from src.ports.language_service_port import LanguageServicePort
from src.services.external_task_wake_task import ExternalTaskWakeTask
from src.services.inactive_user_check import InactiveUserCheck
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService
from tests.unit.services.test_external_task_delivery_task import RecordingChannel, RecordingSessions
from tests.unit.services.test_external_task_wake import URL, World as WakeWorld


class World(WakeWorld):
    async def seed(self) -> "World":
        await super().seed()
        self.channel = RecordingChannel()
        self.factory = MagicMock()
        self.factory.create.return_value = self.channel
        language = AsyncMock(spec=LanguageServicePort)
        language.resolve_ui_language.return_value = LanguageCode.EN
        self.notifications = UserNotificationService(
            state_repo=AsyncMock(), channel_factory=self.factory, coordinator=MagicMock(), notification_sla={},
            session_store=RecordingSessions(), localization=LocalizationService(FileLocalizationAdapter()),
            language=language, inactive_users=InactiveUserCheck(self.users))
        self.runner = ExternalTaskWakeTask(external_tasks=self.service, notification_service=self.notifications)
        await self.target()
        await self.create()
        return self

    def body(self) -> Dict[str, Any]:
        return self.wakes()[-1]


@pytest.fixture
async def world() -> World:
    return await World().seed()


async def test_a_body_that_is_not_a_wake_payload_is_400(world):
    assert await world.runner({"user_id": "usr-1"}, {}) == ({"error": "not a wake payload"}, 400)
    bad = dict(world.body(), generation="many")
    assert (await world.runner(bad, {}))[1] == 400


async def test_a_sent_ping_and_a_stale_one_answer_200(world):
    assert await world.runner(world.body(), {}) == ({"status": "sent"}, 200)
    stale = dict(world.body(), generation=0)
    assert await world.runner(stale, {}) == ({"status": "skipped"}, 200)
    assert len(world.webhook.sent) == 1


async def test_a_target_that_is_down_answers_503_until_the_attempt_bound_then_gives_up(world):
    world.webhook.answer = WakeRefusal.UNREACHABLE
    assert await world.runner(world.body(), {}) == ({"status": "retry"}, 503)
    assert await world.runner(world.body(), {"X-Cloudtasks-Taskretrycount": "1"}) == ({"status": "retry"}, 503)
    last = str(WAKE_MAX_ATTEMPTS - 1)
    assert await world.runner(world.body(), {"x-cloudtasks-taskretrycount": last}) == ({"status": "gave_up"}, 200)
    assert await world.runner(world.body(), {"X-CloudTasks-TaskRetryCount": "junk"}) == ({"status": "retry"}, 503)
    assert world.channel.literals == []


async def test_a_refused_target_is_final_logged_once_and_the_owner_gets_one_literal_notice(world, caplog):
    world.webhook.answer = WakeRefusal.ADDRESS
    with caplog.at_level(logging.INFO):
        answer = await world.runner(world.body(), {})
    assert answer == ({"status": "refused", "code": "address"}, 200)
    assert len(world.channel.literals) == 1
    notice = world.channel.literals[0]
    assert "*research*" in notice and "`address`" in notice
    assert "hooks.example.com" not in notice and URL not in notice
    refusals = [r.getMessage() for r in caplog.records if "wake refused" in r.getMessage()]
    assert len(refusals) == 1 and "code=address" in refusals[0]
    assert URL not in caplog.text and "s3cret" not in caplog.text
    world.factory.create.assert_called_with(platform="slack", channel_id="C-garden")


async def test_a_refusal_with_no_channel_or_no_localization_is_still_final(world):
    world.webhook.answer = WakeRefusal.CLIENT_ERROR
    world.factory.create.return_value = None
    assert (await world.runner(world.body(), {}))[1] == 200
    world.notifications._localization = None
    assert (await world.runner(world.body(), {}))[1] == 200
    assert world.channel.literals == []


async def test_the_retry_count_is_found_among_other_headers(world):
    world.webhook.answer = WakeRefusal.SERVER_ERROR
    headers = {"Content-Type": "application/json", "X-CloudTasks-TaskRetryCount": str(WAKE_MAX_ATTEMPTS - 1)}
    assert await world.runner(world.body(), headers) == ({"status": "gave_up"}, 200)


class _RefusedWithoutOrigin:
    async def on_wake(self, payload):
        from src.domain.external_task_jobs import WakeOutcome, WakeRun
        return WakeRun(WakeOutcome.REFUSED, refusal=WakeRefusal.CLIENT_ERROR)


async def test_a_refusal_that_names_no_channel_posts_nothing(world):
    runner = ExternalTaskWakeTask(external_tasks=_RefusedWithoutOrigin(), notification_service=world.notifications)
    assert await runner(world.body(), {}) == ({"status": "refused", "code": "client_error"}, 200)
    assert world.channel.literals == []
