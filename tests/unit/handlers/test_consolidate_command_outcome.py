"""Facts S2 · T7: `$consolidate` words what the run honestly did.

`consolidate_now` runs the oldest pending batch, which after a backlog is not this command's, so the alert reports
batches (`run.processed`) and a deferral or a continuation, never "N messages were processed"."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.consolidation_run import ConsolidationRun
from src.domain.messaging import MessageContext
from src.handlers.conversation_handler import ConversationHandler

_USER = "user-1"


async def _consolidate(run, *, with_notifications=True):
    queue = MagicMock(enqueue_batch=AsyncMock(return_value="b1"))
    message = MagicMock(role="user", created_at="2026-01-01T00:00:00")
    message.parts = [MagicMock(text="hello", consolidation_text=None)]
    session = MagicMock(user_id=_USER, session_id="s1", messages=[message])
    session.extract_oldest_messages = MagicMock(return_value=[message, message, message, message, message])
    store = MagicMock(load_session=AsyncMock(return_value=session), save_session=AsyncMock())
    agent_factory = MagicMock(get_session_store=MagicMock(return_value=store), ensure_agents_for_user=AsyncMock())
    overflow = AsyncMock(return_value=run)
    notifications = MagicMock(notify=AsyncMock()) if with_notifications else None
    handler = ConversationHandler(
        coordinator=MagicMock(), agent_factory=agent_factory, file_service=MagicMock(), consolidation_queue=queue,
        overflow_callback=overflow, notification_service=notifications,
    )
    channel = MagicMock(send_message=AsyncMock(), send_status=AsyncMock())
    context = MessageContext(text="$consolidate", session_id="s1", user_id=_USER, account_id="a1",
                             metadata={"channel": "C1", "platform": "slack"})

    await handler.handle_command("consolidate", context, channel)

    return overflow, notifications, channel


async def _alert(run):
    overflow, notifications, _ = await _consolidate(run)
    overflow.assert_awaited_once_with(user_id=_USER)
    return notifications.notify.await_args.kwargs["system_alert"]


async def test_a_processed_run_reports_its_batches_and_not_the_commands_messages():
    alert = await _alert(ConsolidationRun(processed=2))

    assert "2 batch(es) consolidated now" in alert
    assert "messages were processed" not in alert and "5 messages" not in alert
    assert "background" not in alert and "another memory run" not in alert


async def test_a_deferred_run_says_it_is_queued_behind_another_memory_run():
    alert = await _alert(ConsolidationRun(deferred=True))

    assert "queued behind another memory run in progress" in alert
    assert "consolidated now" not in alert and "complete" not in alert


async def test_a_run_with_more_pending_says_the_rest_continues_in_the_background():
    alert = await _alert(ConsolidationRun(processed=1, has_more=True))

    assert "1 batch(es) consolidated now" in alert
    assert "The rest continues in the background" in alert


async def test_a_run_that_consolidated_nothing_does_not_claim_success():
    alert = await _alert(ConsolidationRun(processed=0))

    assert "no batch was consolidated in this request" in alert
    assert "consolidated now" not in alert and "complete" not in alert.lower()


async def test_without_notifications_the_channel_gets_the_same_wording():
    _, _, channel = await _consolidate(ConsolidationRun(processed=3, has_more=True), with_notifications=False)

    sent = channel.send_message.await_args.args[0]
    assert "3 batch(es) consolidated now" in sent and "The rest continues in the background" in sent
    assert "messages processed" not in sent


async def test_the_alert_goes_back_to_the_channel_the_command_came_from():
    _, notifications, _ = await _consolidate(ConsolidationRun(processed=1))

    kwargs = notifications.notify.await_args.kwargs
    assert (kwargs["channel_id_override"], kwargs["platform_override"]) == ("C1", "slack")
