"""GmailReconnectFlag: the branches the worker-path tests do not reach (composition-root refactor S5ef).

The grant-revoked paths through the daily review and the email indexing are pinned by
`tests/unit/handlers/test_worker_handler_gmail_reconnect*.py` and `tests/unit/test_refresh_after_disconnect.py`.
"""
import logging
from unittest.mock import AsyncMock

from src.composition.connector_registry import ConnectorRegistry
from src.domain.notification_kind import NotificationKind
from src.services.gmail_reconnect_flag import GmailReconnectFlag
from tests.support.oauth_credentials_store import CredentialsStore, creds


def _flag(oauth, connector, notify):
    notification = AsyncMock()
    notification.notify = notify
    return GmailReconnectFlag(oauth_credentials=oauth, mail_connector=connector, ensure_agents=AsyncMock(),
                              notification_service=notification)


async def test_without_a_credential_port_or_a_mail_connector_it_warns_and_changes_nothing(caplog):
    notify = AsyncMock()
    store = CredentialsStore()
    for oauth, connector in ((None, ConnectorRegistry().get("gmail")), (store.repo, None)):
        with caplog.at_level(logging.WARNING):
            await _flag(oauth, connector, notify).flag("user-1", "acct-1")

    warned = [m for m in caplog.messages if m.startswith("[Worker]")]
    assert warned == ["[Worker] Gmail grant revoked but oauth_credentials not configured"] * 2
    notify.assert_not_awaited()


async def test_a_user_with_no_gmail_credential_is_not_notified():
    store = CredentialsStore()
    notify = AsyncMock()

    await _flag(store.repo, ConnectorRegistry().get("gmail"), notify).flag("user-1", "acct-1")

    notify.assert_not_awaited()


async def test_it_flags_the_stored_credential_and_notifies_the_user_once_with_the_reconnect_instruction():
    store = CredentialsStore()
    await store.repo.save(creds("user-1", "gmail"))
    notify = AsyncMock()
    flag = _flag(store.repo, ConnectorRegistry().get("gmail"), notify)

    await flag.flag("user-1", "acct-1")
    await flag.flag("user-1", "acct-1")

    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert (kwargs["kind"], kwargs["user_id"], kwargs["account_id"]) == (NotificationKind.DOCUMENT_DELIVERY, "user-1", "acct-1")
    assert "Cabinet → Gmail → Reconnect" in kwargs["system_alert"]
    [stored] = [c for c in store.docs.values()]
    assert stored["needs_reconnect"] is True

