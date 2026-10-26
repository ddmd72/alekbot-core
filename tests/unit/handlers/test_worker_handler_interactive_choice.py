"""Facts S4 · T8g: `/worker` task_type `interactive_choice` runs an owner's pick on an interactive message (plan §3.20).

The service is a fake with semantics: it records the choices it was given and answers what the real one answers. What is
asserted is the answer and the choices the handler passed on, rebuilt from the payload the Slack route queues."""
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import INTERACTIVE_CHOICE_TASK, InteractiveChoice
from src.domain.messaging import MessageRef
from src.handlers.worker_handler import WorkerHandler

CHOICE = InteractiveChoice(
    binding=InteractiveBinding(kind="fact_question", id="q1"), option_id="ended", platform="slack",
    platform_user_id="U1", message_ref=MessageRef("slack", "D1", "1729760000.000100"),
    at=datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc),
)


class _FakeChoices:
    def __init__(self, answer: Dict[str, Any]) -> None:
        self.answer = answer
        self.seen: List[InteractiveChoice] = []

    async def handle(self, choice: InteractiveChoice) -> Dict[str, Any]:
        self.seen.append(choice)
        return dict(self.answer)


def _worker(choices=None) -> WorkerHandler:
    return WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=MagicMock(),
        notification_service=AsyncMock(),
        consolidation_service=None,
        coordinator=MagicMock(),
        agent_factory=MagicMock(),
        indexed_email_repo=None,
        user_repo=MagicMock(),
        interactive_choice=choices,
    )


def test_the_task_type_is_the_one_the_route_queues():
    assert INTERACTIVE_CHOICE_TASK == "interactive_choice"


@pytest.mark.parametrize("answer", [{"status": "shown"}, {"status": "ignored"}, {"status": "unknown_user"},
                                    {"status": "unknown_kind"}])
async def test_a_queued_choice_reaches_the_service_whole_and_its_result_is_the_answer(answer):
    choices = _FakeChoices(answer)

    body, status = await _worker(choices).handle({"task_type": INTERACTIVE_CHOICE_TASK, **CHOICE.to_payload()})

    assert (body, status) == (answer, 200)
    assert choices.seen == [CHOICE]


@pytest.mark.parametrize("payload", [
    {},
    {"binding": {"kind": "fact_question", "id": "q1"}},
    {**CHOICE.to_payload(), "binding": {"kind": "Fact Question", "id": "q1"}},
    {**CHOICE.to_payload(), "option_id": ""},
    {**CHOICE.to_payload(), "message_ref": {"platform": "telegram", "channel_id": "D1", "message_id": "m1"}},
    {**CHOICE.to_payload(), "at": "not a time"},
])
async def test_a_payload_that_is_not_a_choice_is_a_400_and_nothing_runs(payload):
    choices = _FakeChoices({"status": "shown"})

    body, status = await _worker(choices).handle({"task_type": INTERACTIVE_CHOICE_TASK, **payload})

    assert (body, status) == ({"error": "invalid choice"}, 400)
    assert choices.seen == []


async def test_a_handler_without_the_service_answers_501():
    body, status = await _worker(None).handle({"task_type": INTERACTIVE_CHOICE_TASK, **CHOICE.to_payload()})

    assert (body, status) == ({"error": "interactive choice not configured"}, 501)


async def test_a_service_error_is_not_turned_into_an_answer():
    class _Broken(_FakeChoices):
        async def handle(self, choice):
            raise RuntimeError("the review store is down")

    with pytest.raises(RuntimeError):
        await _worker(_Broken({})).handle({"task_type": INTERACTIVE_CHOICE_TASK, **CHOICE.to_payload()})
