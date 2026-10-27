"""P13 card A: the seam world's own behaviour, asserted on outcomes (posts, stored history, created tasks, stored keys).

Each test names the regression of the harness it catches.
"""
import hashlib

import pytest

from src.domain.agent import AgentIntent, AgentMessage
from src.domain.notification_kind import NotificationKind
from tests.support.seam_world import (
    ACCOUNT, PRIMARY, TELEGRAM, USER, Delegation, SeamWorld, Step, page, require, says,
)


@pytest.fixture
def world(monkeypatch):
    return SeamWorld(monkeypatch)


async def test_a_notice_reaches_the_primary_and_keeps_its_pair_in_that_session(world):
    """Regression: the channel factory keyed wrongly, the session store not wired into the notifier.
    Forward `ums.py:361-364` (resolved channel → factory) → `SeamChannels.create`; `ums.py:376,447-449` → the store."""
    await world.set_primary(*PRIMARY)
    world.agent("quick_response_agent").play(says("Your document is ready."))

    result = await world.notifier.notify(USER, ACCOUNT, "A document finished.", kind=NotificationKind.DOCUMENT_DELIVERY)

    require(not world.unscripted(), "every agent call was scripted")
    assert result.delivered
    assert [p.value for p in world.posts_to(*PRIMARY)] == ["Your document is ready."]
    assert world.channels.posts == world.posts_to(*PRIMARY)
    assert world.history("u1:C1") == [("user", "A document finished.", None),
                                      ("model", "Your document is ready.", "Your document is ready.")]


async def test_both_overrides_win_over_the_primary_and_a_channel_alone_does_not(world):
    """Pins `ums.py:119` as the world sees it: the override is taken only with both channel and platform.
    Forward `channel_id_override`/`platform_override` → `ums.py:119-125`; session `ums.py:376`."""
    await world.set_primary(*PRIMARY)
    world.agent("quick_response_agent").play(says("Done on Telegram."), says("Done on the primary."))

    await world.notifier.notify(USER, ACCOUNT, "A document finished.", kind=NotificationKind.DOCUMENT_DELIVERY,
                                channel_id_override="7001", platform_override="telegram")
    require(not world.unscripted(), "every agent call was scripted")
    assert [p.value for p in world.posts_to(*TELEGRAM)] == ["Done on Telegram."]
    assert world.posts_to(*PRIMARY) == []
    assert [role for role, _, _ in world.history("u1:7001")] == ["user", "model"]
    assert world.history("u1:C1") == []

    await world.notifier.notify(USER, ACCOUNT, "Another document finished.",
                                kind=NotificationKind.DOCUMENT_DELIVERY, channel_id_override="7001")
    require(not world.unscripted(), "every agent call was scripted")
    assert [p.value for p in world.posts_to(*PRIMARY)] == ["Done on the primary."]
    assert len(world.posts_to(*TELEGRAM)) == 1


async def _delegate_a_page(world):
    caller_context = {"user_id": USER, "account_id": ACCOUNT, "session_id": "u1:C1", "thread_id": None}
    world.agent("smart_response_agent").play(
        Step(says("A page is on its way."), Delegation("create_html_page", "q", {"file_ref": "x"})))
    message = AgentMessage.create(sender="test", recipient=f"smart_response_agent_{USER}", intent=AgentIntent.QUERY,
                                  payload={"text": ""}, context=caller_context)
    response = await world.coordinator.route_message(message)
    return caller_context, response


async def test_a_delegation_from_a_stand_in_creates_the_agent_task_as_the_coordinator_does(world):
    """Regression: a stand-in that does not delegate through the real coordinator, or loses the caller's context.
    Forward `seam_world.StandInAgent.process` → `agent_coordinator.py:400,414-415,429,558-564` → `gcp:207-231`."""
    caller_context, response = await _delegate_a_page(world)

    require(not world.unscripted(), "every agent call was scripted")
    require(response.result.text == "A page is on its way.", "the stand-in answered after delegating")
    [request] = world.requests("agent_execution")
    assert request.name is None
    assert request.deadline_s == 1220
    assert request.scheduled is False
    assert request.body["agent_id"] == "html_page_generator_agent"
    assert request.body["intent"] == "create_html_page"
    assert request.body["query"].endswith(" q")
    assert request.body["context"] == {**caller_context, "params": {"file_ref": "x"},
                                       "_call_chain": ["html_page_generator_agent"]}


async def test_running_a_created_body_delivers_the_page_to_the_primary(world):
    """Regression: `run` not closing a chain through `/worker`, the media store or the link service not wired.
    Forward `agent_worker_handler.py:268-272` → `document_delivery_service.py:87-89` → the bucket; `:278-282` →
    `ums.py:504-506,550`."""
    await world.set_primary(*PRIMARY)
    await _delegate_a_page(world)
    world.agent("html_page_generator_agent").play(page("heat-pumps.html", "Heat pumps in Springfield.html"))

    answer = await world.run(world.bodies("agent_execution")[0])

    require(not world.unscripted(), "every agent call was scripted")
    require(answer[1] == 200, "the delivery was answered 200")
    [(label, url)] = world.docs_to(*PRIMARY)
    assert label == "Heat pumps in Springfield.html"
    assert url.startswith("https://svc.test.example/f/")
    [key] = world.stored_keys()
    assert key.startswith("docs/u1/") and key.endswith("-heat-pumps.html")
    [(_, user_text, _), (_, note, _)] = world.history("u1:C1")
    assert user_text == "[System: async document ready — Heat pumps in Springfield.html]"
    assert f'"file_ref": "{key}"' in note


async def test_an_exhausted_stand_in_raises_a_runtime_error_and_records_the_message(world):
    """Regression: an exhausted script that raises an `AssertionError` (a strict xfail would take it for its own)."""
    quick = world.agent("quick_response_agent")
    direct = AgentMessage.create(sender="test", recipient=quick.agent_id, intent=AgentIntent.QUERY, payload={},
                                 context={"user_id": USER})
    with pytest.raises(RuntimeError):
        await quick.process(direct)
    assert quick.unscripted == [direct]


async def test_an_exhausted_stand_in_behind_a_service_shows_only_in_unscripted(world):
    """Regression: an exhausted script that leaves no trace once the coordinator swallowed its raise
    (`agent_coordinator.py:201-210`): the notice posts nothing, `unscripted()` holds the one message."""
    await world.set_primary(*PRIMARY)
    world.agent("quick_response_agent")

    result = await world.notifier.notify(USER, ACCOUNT, "A document finished.", kind=NotificationKind.DOCUMENT_DELIVERY)

    assert not result.delivered
    assert world.channels.posts == []
    [message] = world.unscripted()
    assert message.recipient == f"quick_response_agent_{USER}"
    assert message.context["current_message_parts"][0].text.startswith("[System: A document finished.")


async def test_requests_decode_a_named_delayed_task(world):
    """Regression: `requests()` reading the name or the schedule wrongly. Forward `tds:73-87` → `gcp:380-399`."""
    await world.dispatch.enqueue_worker_task("fire_due_reminders", {}, delay_seconds=30, dedup_key="k")

    [request] = world.requests()
    assert request.name == "fire_due_reminders-" + hashlib.sha256(b"k").hexdigest()[:32]
    assert request.name.endswith("-" + hashlib.sha256(b"k").hexdigest()[:32])
    assert request.scheduled is True
    assert request.deadline_s is None
    assert request.body == {"task_type": "fire_due_reminders"}
