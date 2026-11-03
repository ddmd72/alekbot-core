"""Mutation survivors of conversation_handler, group ch6: the long-turn heartbeat loop."""
from src.domain.messaging import SmartResponse

from tests.unit.handlers.test_conversation_handler_long_turn import (  # noqa: F401
    _build, handler_factory, lt,
)


async def test_failed_heartbeat_does_not_stop_the_cancel_watch(lt):
    # The first heartbeat write fails; the watcher must keep polling, so the
    # cancel flag seen on the next beat still cancels the agent.
    lt.service.heartbeat.side_effect = [RuntimeError("store down"), True]
    lt.route(result=SmartResponse(text="would be the answer"), delay=0.5)
    await lt.handle()
    assert lt.channel.send_late_answer.call_args.args[0] == lt.ui("long_turn_cancelled")
