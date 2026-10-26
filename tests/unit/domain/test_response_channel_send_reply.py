"""`ResponseChannel.send_reply` (Facts S4 · T8f): a channel that does not implement it refuses, naming its platform."""
from types import SimpleNamespace

import pytest

from src.domain.messaging import ResponseChannel


async def test_a_channel_without_a_reply_refuses_and_names_its_platform():
    with pytest.raises(NotImplementedError, match="web cannot reply to a message"):
        await ResponseChannel.send_reply(SimpleNamespace(platform="web"), "hello", "55")  # type: ignore[arg-type]
