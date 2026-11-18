"""Mutation survivors of conversation_handler, group ch6: the long-turn heartbeat loop."""
import asyncio

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.long_turn import LongTurnStatus
from src.domain.messaging import SmartResponse

from tests.unit.handlers.test_conversation_handler_long_turn import (  # noqa: F401
    _build, handler_factory, lt,
)

# Only a hang guard: the outcome below never depends on timing. A watcher that stops after the failed beat leaves the
# agent waiting forever, and this turns that hang into a failure.
HANG_GUARD_S = 30.0


async def test_failed_heartbeat_does_not_stop_the_cancel_watch(lt):
    # The first heartbeat write fails; the watcher must keep polling, so the
    # cancel flag seen on the next beat still cancels the agent. The agent never
    # finishes on its own (it waits on a gate nothing opens), so only that cancel
    # can end the turn: no race against the heartbeat timer.
    lt.service.heartbeat.side_effect = [RuntimeError("store down"), True]
    never_opened = asyncio.Event()

    async def _route(message):
        await never_opened.wait()
        return AgentResponse(task_id="t", agent_id="smart", status=AgentStatus.SUCCESS,
                             result=SmartResponse(text="would be the answer"), confidence=1.0, metadata={})
    lt.coordinator.route_message.side_effect = _route

    await asyncio.wait_for(lt.handle(), HANG_GUARD_S)
    assert lt.service.heartbeat.await_count == 2
    assert lt.channel.send_late_answer.call_args.args[0] == lt.ui("long_turn_cancelled")
    lt.service.finish.assert_awaited_with(lt.turn_id, LongTurnStatus.CANCELLED)
