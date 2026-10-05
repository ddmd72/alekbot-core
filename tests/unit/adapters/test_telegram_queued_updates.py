"""
Unit tests for Telegram → Cloud Tasks hand-off (Task 10, LONG_RUNNING_TURNS_RFC §5.8).

A chat turn may run ~25 min; an inline webhook loses its CPU once Telegram drops the
connection. The webhook now hands the update off to a Cloud Task (`task_type=
"telegram_update"`) and returns immediately; the worker side (`handle_queued_update`)
re-parses the raw update and processes it with turn identity (`turn_id`,
`origin_message_id`, `event_time`) so ConversationHandler's long-turn tracking and
late-answer delivery work the same as on the inline path.
"""
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update

from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter

UPDATE = {"update_id": 501, "message": {"message_id": 15, "date": 1700000000,
          "chat": {"id": 42, "type": "private"}, "from": {"id": 7, "is_bot": False, "first_name": "D"},
          "text": "hello"}}


def _adapter(task_queue):
    dedup = MagicMock()
    dedup.try_mark_processed = AsyncMock(return_value=True)
    return TelegramWebhookAdapter(token="1:abc", webhook_secret="s", dedup_store=dedup,
                                  session_store=MagicMock(), conversation_handler=AsyncMock(),
                                  iam_service=AsyncMock(), task_queue=task_queue)


async def test_webhook_enqueues_and_returns_at_once():
    queue = AsyncMock()
    adapter = _adapter(queue)
    adapter._verify_webhook_signature = AsyncMock(return_value=True)
    adapter._process_message = AsyncMock()
    req = MagicMock()
    req.get_json = AsyncMock(return_value=UPDATE)
    # Quart's `request`/`jsonify` are context-local proxies; `patch(..., new=...)` swaps
    # the module attribute directly (mock.patch's default autospec path forces a context
    # lookup outside a real request/app context and raises RuntimeError).
    with patch("src.adapters.telegram.webhook_adapter.request", new=req), \
            patch("src.adapters.telegram.webhook_adapter.jsonify", side_effect=lambda x: x):
        _, status = await adapter._handle_telegram_update()
    assert status == 200
    kwargs = queue.enqueue_worker_task.call_args.kwargs
    assert kwargs["task_type"] == "telegram_update"
    assert kwargs["payload"] == {"update": UPDATE}
    assert kwargs["deadline_seconds"] == 1800
    adapter._process_message.assert_not_awaited()


async def test_without_a_queue_processing_stays_inline():
    adapter = _adapter(None)
    adapter._verify_webhook_signature = AsyncMock(return_value=True)
    adapter._process_message = AsyncMock()
    req = MagicMock()
    req.get_json = AsyncMock(return_value=UPDATE)
    with patch("src.adapters.telegram.webhook_adapter.request", new=req), \
            patch("src.adapters.telegram.webhook_adapter.jsonify", side_effect=lambda x: x):
        await adapter._handle_telegram_update()
    adapter._process_message.assert_awaited()


async def test_queued_update_is_processed_with_turn_identity():
    adapter = _adapter(AsyncMock())
    adapter._process_message = AsyncMock()
    body, status = await adapter.handle_queued_update({"task_type": "telegram_update", "update": UPDATE})
    assert status == 200
    message, = adapter._process_message.call_args.args
    assert adapter._process_message.call_args.kwargs["update_id"] == 501
    assert message.message_id == 15


async def test_process_message_passes_turn_identity_metadata_to_conversation_handler():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="account-1")
    iam_service.authorize.return_value = decision

    adapter = TelegramWebhookAdapter(
        token="1:abc", webhook_secret="s", dedup_store=MagicMock(),
        session_store=MagicMock(), conversation_handler=conversation_handler,
        iam_service=iam_service, task_queue=None,
    )

    update = Update.de_json(UPDATE, adapter.bot)
    await adapter._process_message(update.message, update_id=501)

    conversation_handler.handle_message.assert_awaited_once()
    context = conversation_handler.handle_message.call_args.args[0]
    assert context.metadata["turn_id"] == "telegram:501"
    assert context.metadata["origin_message_id"] == "15"
    assert context.metadata["event_time"] == 1700000000.0
