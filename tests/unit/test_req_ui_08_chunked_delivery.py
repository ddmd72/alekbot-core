import pytest
from unittest.mock import AsyncMock
from src.adapters.slack.response_channel import SlackResponseChannel, SLACK_CHUNK_SIZE


@pytest.mark.requirement("REQ-UI-08")
@pytest.mark.asyncio
async def test_chunked_delivery_single_chunk_updates_message():
    """
    Verify chunked delivery updates the original message when only one chunk is needed.
    Covers: REQ-UI-08 (Chunked Response Delivery)

    Self-validation:
    - Calls real chunking logic via send_chunked_message: ✅
    - Fails if update_message is not used for single chunk: ✅
    """
    client = AsyncMock()
    channel = SlackResponseChannel(client, "C1", "token")
    channel.update_message = AsyncMock()
    channel.send_message = AsyncMock()

    await channel.send_chunked_message("short reply", "msg-1", thread_id="thread-1")

    channel.update_message.assert_called_once_with("msg-1", "short reply")
    channel.send_message.assert_not_called()


@pytest.mark.requirement("REQ-UI-08")
@pytest.mark.asyncio
async def test_chunked_delivery_multiple_chunks_posts_thread():
    """
    Verify long responses are split and posted as thread chunks.
    Covers: REQ-UI-08 (Chunked Response Delivery)

    Self-validation:
    - Uses real _split_into_chunks output: ✅
    - Fails if chunks are not posted or header update missing: ✅
    """
    client = AsyncMock()
    channel = SlackResponseChannel(client, "C1", "token")
    channel.update_message = AsyncMock()
    channel.send_message = AsyncMock()

    long_text = "A" * (SLACK_CHUNK_SIZE + 25)
    expected_chunks = channel._split_into_chunks(long_text, SLACK_CHUNK_SIZE)

    await channel.send_chunked_message(long_text, "msg-2", thread_id=None)

    channel.update_message.assert_called_once_with("msg-2", "✅ Відповідь готова.")

    assert channel.send_message.await_count == len(expected_chunks)
    for chunk in expected_chunks:
        channel.send_message.assert_any_call(chunk, "msg-2")
