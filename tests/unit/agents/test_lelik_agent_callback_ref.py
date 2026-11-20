from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest

from tests.unit.agents.test_lelik_agent import _build_agent


@pytest.mark.asyncio
async def test_execute_puts_the_reference_in_both_callback_urls():
    telephony = AsyncMock()
    telephony.originate_call.return_value = "CA0001"
    agent = _build_agent(telephony)

    await agent.execute(
        purpose="user asked to talk", callback_ref="Zm9v-_", answer_url="https://main.example.com/voice/answer",
        to_number="+15550101",
    )

    kwargs = telephony.originate_call.await_args.kwargs
    for url in (kwargs["answer_url"], kwargs["status_callback_url"]):
        assert parse_qs(urlsplit(url).query) == {"ref": ["Zm9v-_"]}
