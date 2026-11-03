"""Mutation-survivor tests for ConversationHandler.handle_message: the router message's agent context and attachment
summary, temp-file cleanup that survives one failed removal, and the origin platform handed to the Quick fallback."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentResponse
from src.domain.messaging import FileAttachment, MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler


def _handler(fallback_service=None):
    response = AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Hi."))
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=response)
    handler = ConversationHandler(coordinator=coordinator, agent_factory=factory, file_service=MagicMock(),
                                  global_config=ConsolidationSettings(threshold=50, batch_size=30),
                                  fallback_service=fallback_service)
    return handler, coordinator


def _channel(platform="slack"):
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = platform
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content",
                 "update_status_with_phrase_and_dots"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.download_file = AsyncMock(return_value=None)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _ctx(attachments=(), thread_id=None):
    return MessageContext(text="hello", session_id="s", user_id="u", account_id="a",
                          attachments=list(attachments), thread_id=thread_id, metadata={})


async def _run(handler, ctx, channel):
    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
        await handler.handle_message(ctx, channel)


async def test_thread_id_reaches_the_router_agent_context():
    handler, coordinator = _handler()
    await _run(handler, _ctx(thread_id="thread-1"), _channel())
    message = coordinator.route_message.await_args.args[0]
    assert message.context["thread_id"] == "thread-1"


async def test_attachment_summary_reaches_the_router_payload():
    handler, coordinator = _handler()
    attachment = FileAttachment(url="", mime_type="image/png", filename="file-1.png", size_bytes=42)
    await _run(handler, _ctx(attachments=[attachment]), _channel())
    message = coordinator.route_message.await_args.args[0]
    assert message.payload["attachments"] == [
        {"filename": "file-1.png", "mime_type": "image/png", "size_bytes": 42}
    ]


async def test_one_failed_temp_file_removal_does_not_stop_the_cleanup_of_the_rest(tmp_path):
    first, second = tmp_path / "file-1.png", tmp_path / "file-2.png"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    handler, _ = _handler()
    channel = _channel()
    channel.download_file = AsyncMock(side_effect=[str(first), str(second)])
    attachments = [FileAttachment(url=f"https://example.invalid/{n}", mime_type="image/png", filename=n)
                   for n in ("file-1.png", "file-2.png")]
    real_remove = os.remove

    def flaky_remove(path):
        if str(path) == str(first):
            raise OSError("busy")
        real_remove(path)

    with patch("src.handlers.conversation_handler.os.remove", side_effect=flaky_remove):
        await _run(handler, _ctx(attachments=attachments), channel)
    assert first.exists()
    assert not second.exists()


async def test_the_origin_platform_is_forwarded_to_the_quick_fallback():
    response = AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Hi."))
    fallback = MagicMock()
    fallback.try_quick_fallback = AsyncMock(return_value=response)
    handler, _ = _handler(fallback_service=fallback)
    await _run(handler, _ctx(), _channel(platform="telegram"))
    assert fallback.try_quick_fallback.await_args.kwargs["origin_platform"] == "telegram"
