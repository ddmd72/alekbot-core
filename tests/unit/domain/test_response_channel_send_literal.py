"""`ResponseChannel.send_literal`: a channel that does not implement it refuses, naming its platform (S4 review A4)."""
from types import SimpleNamespace

import pytest

from src.domain.messaging import ResponseChannel


async def test_a_channel_without_a_literal_send_refuses_and_names_its_platform():
    with pytest.raises(NotImplementedError, match="web cannot send literal text"):
        await ResponseChannel.send_literal(SimpleNamespace(platform="web"), "hello")  # type: ignore[arg-type]
