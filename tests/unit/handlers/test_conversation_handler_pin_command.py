"""ConversationHandler: a `$<digits>` code and `$pin cancel` in chat (CABINET_DEVICE_SESSIONS_RFC §7.9, §7.7) end to end
over the PIN world (`tests/support/pin_lock_world.py`: the real `FactsPinService`, `AccountLockService`,
`DeviceSessionService` and `UserNotificationService` on fakes with semantics).

The digits branch comes first: the duress code locks the account and the reply is the lock notice; the real PIN clears
the counter and the reply says to change it; another code counts ("Wrong code (n left)"), the fifth in a row locks; a
user without a PIN gets the fixed no-PIN line and nothing is counted. The code is never echoed in a reply and never
logged (only the outcome is); `$pin cancel` ends a pending reset.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.services.localization_service import LocalizationService
from tests.support.pin_lock_world import DURESS, PIN, WRONG, PinLockWorld, en

CODES = (PIN, DURESS, WRONG)


class Env:
    def __init__(self, facts_pin="world") -> None:
        self.world = PinLockWorld()
        service = self.world.service if facts_pin == "world" else facts_pin
        self.handler = ConversationHandler(
            coordinator=MagicMock(), agent_factory=MagicMock(),
            global_config=ConsolidationSettings(threshold=50, batch_size=30),
            localization=LocalizationService(FileLocalizationAdapter()),
            commands=CommandServices(facts_pin=service))

    async def send(self, command: str) -> list:
        """The replies to `$<command>` typed in alex's chat (English UI)."""
        channel = MagicMock()
        channel.send_message = AsyncMock()
        context = MessageContext(text=f"${command}", session_id="user-alex:C-chat", user_id="user-alex",
                                 account_id="acct-alex", language="en", attachments=[],
                                 metadata={"platform": "slack", "channel": "C-chat"})
        await self.handler.handle_command(command, context, channel)
        return [c.args[0] for c in channel.send_message.await_args_list]

    def wrong_in_row(self) -> int:
        return self.world.pins.records["user-alex"].wrong_in_row


@pytest.fixture
async def env() -> Env:
    e = Env()
    await e.world.seed()
    await e.world.set_pin()
    return e


async def test_a_user_without_a_pin_gets_the_fixed_line_and_nothing_is_counted():
    e = Env()
    await e.world.seed()

    assert await e.send(PIN) == [en(UIMessage.FACTS_PIN_CHAT_NO_PIN)]
    assert e.world.pins.records == {} and e.world.flag() == (True, None)


async def test_a_wrong_code_counts_and_says_how_many_are_left(env):
    assert await env.send(WRONG) == [en(UIMessage.FACTS_PIN_CHAT_WRONG, left="4")]
    assert await env.send(WRONG) == [en(UIMessage.FACTS_PIN_CHAT_WRONG, left="3")]
    assert env.wrong_in_row() == 2 and env.world.flag() == (True, None)


async def test_the_real_pin_clears_the_counter_and_says_to_change_it(env):
    await env.send(WRONG)

    assert await env.send(PIN) == [en(UIMessage.FACTS_PIN_CHAT_REAL_PIN)]
    assert env.wrong_in_row() == 0 and env.world.flag() == (True, None)


async def test_the_fifth_wrong_code_locks_the_account_and_the_reply_is_the_lock_notice(env):
    for _ in range(4):
        await env.send(WRONG)

    replies = await env.send(WRONG)

    notice = en(UIMessage.ACCOUNT_LOCKED_PIN_ATTEMPTS_NOTICE)
    assert replies == [notice]
    assert env.world.flag() == (False, "pin_attempts")
    assert ("slack", "C-alex", notice) in env.world.sent and ("telegram", "chat-alex", notice) in env.world.sent


async def test_the_duress_code_locks_the_account_revokes_everything_and_replies_with_the_lock_notice(env):
    replies = await env.send(DURESS)

    notice = en(UIMessage.ACCOUNT_LOCKED_DURESS_NOTICE)
    assert replies == [notice]
    assert env.world.flag() == (False, "duress")
    assert env.world.devices.sessions["sid-alex"].revoked_at is not None
    assert env.world.token_revoked("user-alex") and not env.world.token_revoked("user-other")
    assert ("slack", "C-alex", notice) in env.world.sent


async def test_a_code_on_an_account_already_locked_gets_the_plain_lock_line(env):
    await env.send(DURESS)

    assert await env.send(PIN) == [en(UIMessage.FACTS_PIN_CHAT_LOCKED)]


async def test_a_non_ascii_code_is_answered_as_a_wrong_code_and_never_echoed(env):
    replies = await env.send("４０７１")

    assert replies == [en(UIMessage.FACTS_PIN_CHAT_WRONG, left="4")]
    assert "４０７１" not in replies[0]


async def test_without_the_service_the_code_gets_the_unavailable_line():
    e = Env(facts_pin=None)

    assert await e.send(PIN) == [en(UIMessage.FACTS_PIN_CHAT_UNAVAILABLE)]


async def test_a_failing_check_gets_the_unavailable_line_and_logs_the_error_type_only(caplog):
    failing = MagicMock()
    failing.check = AsyncMock(side_effect=RuntimeError(f"store down while checking {PIN}"))
    e = Env(facts_pin=failing)

    with caplog.at_level(logging.DEBUG):
        replies = await e.send(PIN)

    assert replies == [en(UIMessage.FACTS_PIN_CHAT_UNAVAILABLE)]
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert all(PIN not in r.getMessage() for r in caplog.records)


async def test_the_branch_logs_the_outcome_and_never_a_code(env, caplog):
    with caplog.at_level(logging.DEBUG):
        replies = [*await env.send(WRONG), *await env.send(PIN), *await env.send(DURESS)]

    messages = [r.getMessage() for r in caplog.records]
    assert all(code not in m for m in messages for code in CODES)
    assert all(code not in reply for reply in replies for code in CODES)
    branch = [m for m in messages if "[ConversationHandler] $<code>" in m]
    assert [m.rsplit(": ", 1)[1] for m in branch] == ["wrong", "ok", "duress"]


# ---------------------------------------------------------------------------------------------- `$pin cancel`

async def test_pin_cancel_ends_a_pending_reset(env):
    session = await env.world.add_session("sid-reset")
    assert await env.world.service.request_reset(session) is not None

    assert await env.send("pin cancel") == [en(UIMessage.FACTS_PIN_RESET_CANCELLED)]
    assert env.world.pins.records["user-alex"].reset_requested_at is None


async def test_pin_cancel_without_a_pending_reset_says_so(env):
    assert await env.send("pin cancel") == [en(UIMessage.FACTS_PIN_NO_RESET_PENDING)]


@pytest.mark.parametrize("command", ["pin", "pin reset", "pin cancel now"])
async def test_another_pin_command_gets_the_usage_line(env, command):
    assert await env.send(command) == [en(UIMessage.FACTS_PIN_USAGE)]


async def test_pin_cancel_without_the_service_gets_the_unavailable_line():
    e = Env(facts_pin=None)

    assert await e.send("pin cancel") == [en(UIMessage.FACTS_PIN_CHAT_UNAVAILABLE)]


async def test_a_failing_pin_cancel_gets_the_unavailable_line():
    failing = MagicMock()
    failing.cancel_reset = AsyncMock(side_effect=RuntimeError("store down"))
    e = Env(facts_pin=failing)

    assert await e.send("pin cancel") == [en(UIMessage.FACTS_PIN_CHAT_UNAVAILABLE)]
