"""VoiceCallSetupService.prepare: a store failure on the ticket writes (after the persona was
assembled) is handled like a persona failure — release, alert, VoiceCallSetupError — instead of
escaping with the one-call marker still held (final-review finding 8)."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.secret_digest import fingerprint
from src.services.voice_call_setup_service import VoiceCallSetupError, VoiceCallSetupService
from tests.support.voice_tickets import call_kind_key, ticket_key


def _agent():
    agent = MagicMock()
    agent.session_config = AsyncMock(return_value={"instructions": "you are Lelik", "tools": []})
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_key", [ticket_key("t1"), call_kind_key("t1")], ids=["ticket", "call_kind"])
async def test_ticket_write_failure_releases_alerts_and_raises(failing_key, caplog):
    store = AsyncMock()

    async def set_(key, value, ttl_s):
        if key == failing_key:
            raise RuntimeError("firestore unavailable")

    store.set.side_effect = set_
    alert = AsyncMock()
    service = VoiceCallSetupService(store, alert, AsyncMock(return_value=_agent()), digest_key=b"k" * 32)

    with pytest.raises(VoiceCallSetupError):
        await service.prepare("t1", "u1", "a1", "phone")

    deleted = {c.args[0] for c in store.delete.await_args_list}
    assert deleted == {ticket_key("t1"), "voice_one_call:u1"}
    alert.post.assert_awaited_once()
    failure_lines = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("u1" in line and f"(ticket {fingerprint('t1')})" in line for line in failure_lines)


@pytest.mark.asyncio
async def test_a_release_that_also_fails_still_alerts_and_raises_the_setup_error():
    store = AsyncMock()
    store.set.side_effect = RuntimeError("firestore unavailable")
    store.delete.side_effect = RuntimeError("firestore unavailable")
    alert = AsyncMock()
    service = VoiceCallSetupService(store, alert, AsyncMock(return_value=_agent()), digest_key=b"k" * 32)

    with pytest.raises(VoiceCallSetupError):
        await service.prepare("t1", "u1", "a1", "phone")
    alert.post.assert_awaited_once()
