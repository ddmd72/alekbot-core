"""ConversationHandler: `$devices` and `$devices off` (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6) end to end over the real
`DeviceSessionService` on the in-memory repository: the list (only the owner's live sessions, a hostile label shown
as one inert line), the kill switch (every session of the owner revoked as `kill_switch`, the reply carrying the count
and the lost-phone checklist, other users untouched), the empty list, usage, no service, a failing service, and all of
it in each of the four languages' strings."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.billing import AccountTier, BillingAccount
from src.domain.device_session import SessionAuthKind, SessionRevokeReason
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.domain.user import UserProfile
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.locales import en, es, fr, uk
from src.services.device_session_service import DeviceSessionService
from src.services.session_service import SessionService
from tests.support.device_session_repo import FakeDeviceSessionRepository

ALEX = UserProfile(user_id="user-alex", external_user_id="firebase|alex", email="alex@example.com",
                   display_name="Alex", account_id="acct-alex")
OTHER = UserProfile(user_id="user-other", external_user_id="firebase|other", email="other@example.com",
                    display_name="Other", account_id="acct-other")
ACCOUNT = BillingAccount(account_id="acct-alex", tier=AccountTier.FAMILY, iam_policy={"user-alex": "owner"})
OTHER_ACCOUNT = BillingAccount(account_id="acct-other", tier=AccountTier.FAMILY, iam_policy={"user-other": "owner"})
PHONE, MAC = "iPhone — Home Screen (call)", "Mac — Chrome"


class Env:
    def __init__(self, commands_service=True):
        self.repo = FakeDeviceSessionRepository()
        notifications = AsyncMock()
        notifications.notify_ui_notice.return_value = True
        self.service = DeviceSessionService(self.repo, SessionService(secret_key="test-secret-key-must-be-32-characters-minimum"),
                                            notifications)
        self.handler = ConversationHandler(
            coordinator=MagicMock(), agent_factory=MagicMock(),
            global_config=ConsolidationSettings(threshold=50, batch_size=30),
            commands=CommandServices(device_sessions=self.service if commands_service else None))

    async def sign_in(self, label, user=ALEX, account=ACCOUNT):
        session, _, _ = await self.service.start(user, account, label=label, platform_hint="x",
                                                 auth_kind=SessionAuthKind.GOOGLE)
        return session

    def live(self, user_id):
        return [s for s in self.repo.sessions.values() if s.user_id == user_id and s.revoked_at is None]


async def run(env, command, language=None, user=ALEX):
    channel = MagicMock()
    channel.send_message = AsyncMock()
    channel.language = language
    context = MessageContext(text=f"${command}", session_id=f"{user.user_id}:C1", user_id=user.user_id,
                             account_id=user.account_id, attachments=[], metadata={})
    await env.handler.handle_command(command, context, channel)
    return [c.args[0] for c in channel.send_message.await_args_list]


def ui(message, locale=uk, **fmt):
    return locale.UI_STRINGS[message.value].format(**fmt) if fmt else locale.UI_STRINGS[message.value]


async def test_list_shows_the_owners_live_sessions_only():
    env = Env()
    await env.sign_in(PHONE)
    mac = await env.sign_in(MAC)
    await env.sign_in("Their laptop", user=OTHER, account=OTHER_ACCOUNT)
    await env.service.revoke(ALEX.user_id, mac.sid, SessionRevokeReason.LOGOUT)
    await env.sign_in("iPad")

    [text] = await run(env, "devices")

    lines = text.split("\n")
    assert lines[0] == ui(UIMessage.DEVICES_LIST_HEADER, count=2)
    assert PHONE in lines[1] and "google" in lines[1] and "UTC" in lines[1]
    assert "iPad" in lines[2]
    assert MAC not in text and "Their laptop" not in text


async def test_a_hostile_label_is_shown_as_one_plain_capped_line():
    env = Env()
    await env.sign_in("<@here> `evil`\n[x](http://bad.example)" + "z" * 200)

    [text] = await run(env, "devices")

    label_line = text.split("\n")[1]
    assert len(text.split("\n")) == 2          # the newline of the label did not start a line
    assert not set("<>`") & set(label_line)
    assert "z" * 81 not in label_line


async def test_an_empty_list_says_so():
    [text] = await run(Env(), "devices")

    assert text == ui(UIMessage.DEVICES_LIST_EMPTY)


async def test_off_revokes_every_session_of_the_owner_and_replies_with_count_and_checklist():
    env = Env()
    phone, mac = await env.sign_in(PHONE), await env.sign_in(MAC)
    theirs = await env.sign_in("Their laptop", user=OTHER, account=OTHER_ACCOUNT)

    [text] = await run(env, "devices off")

    assert text == ui(UIMessage.DEVICES_OFF_DONE, count=2) + "\n\n" + ui(UIMessage.DEVICES_CHECKLIST)
    assert "$devices off" in text and "Apple" in text and "Telegram" in text
    assert env.live(ALEX.user_id) == []
    assert {env.repo.sessions[s.sid].revoke_reason for s in (phone, mac)} == {SessionRevokeReason.KILL_SWITCH}
    assert [s.sid for s in env.live(OTHER.user_id)] == [theirs.sid]


async def test_off_with_no_session_reports_zero():
    [text] = await run(Env(), "devices off")

    assert text.startswith(ui(UIMessage.DEVICES_OFF_DONE, count=0))


async def test_any_other_argument_prints_usage_and_revokes_nothing():
    env = Env()
    await env.sign_in(PHONE)

    [text] = await run(env, "devices off now")

    assert text == ui(UIMessage.DEVICES_USAGE)
    assert len(env.live(ALEX.user_id)) == 1


async def test_without_the_service_the_command_says_unavailable():
    [text] = await run(Env(commands_service=False), "devices")

    assert text == ui(UIMessage.DEVICES_UNAVAILABLE)


async def test_a_failing_service_says_unavailable_and_does_not_raise():
    env = Env()
    env.service.revoke_all = AsyncMock(side_effect=RuntimeError("store down"))

    [text] = await run(env, "devices off")

    assert text == ui(UIMessage.DEVICES_UNAVAILABLE)


@pytest.mark.parametrize("language, locale", [("uk", uk), ("en", en), ("fr", fr), ("es", es)])
def test_every_language_has_every_devices_string(language, locale):
    for message in (UIMessage.DEVICES_UNAVAILABLE, UIMessage.DEVICES_USAGE, UIMessage.DEVICES_LIST_HEADER,
                    UIMessage.DEVICES_LIST_EMPTY, UIMessage.DEVICES_LIST_LINE, UIMessage.DEVICES_OFF_DONE,
                    UIMessage.DEVICES_CHECKLIST):
        assert locale.UI_STRINGS[message.value].strip()
    assert "$devices off" in locale.UI_STRINGS[UIMessage.DEVICES_CHECKLIST.value]
