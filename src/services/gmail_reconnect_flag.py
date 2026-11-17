"""The Gmail reconnect flag: what happens when Google rejects the user's refresh token.

Held by `WorkerHandler` (email indexing) and by the daily email review runner, the two background Gmail paths.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from ..domain.connector_spec import ConnectorSpec
from ..domain.notification_kind import NotificationKind
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService


class GmailReconnectFlag:
    """Marks the Gmail credentials `needs_reconnect` and tells the user, once per breakage."""

    def __init__(
        self,
        *,
        oauth_credentials: Optional[OAuthCredentialsPort],
        # the mail kind's spec: the Gmail credential flagged is the one of its kind
        mail_connector: Optional[ConnectorSpec],
        ensure_agents: Callable[[str], Awaitable[Any]],
        notification_service: "UserNotificationService",
    ) -> None:
        self._oauth_credentials = oauth_credentials
        self._mail_connector = mail_connector
        self._ensure_agents = ensure_agents
        self._notification = notification_service

    async def flag(self, user_id: str, account_id: str) -> None:
        """Google rejected the user's Gmail refresh token: mark the credentials
        `needs_reconnect` (Cabinet shows it) and tell the user once per breakage.

        Only the transition notifies — the daily review and the auto-index keep
        hitting the dead token every day until the user reconnects, and that must
        not become a daily message. A reconnect saves fresh credentials, which
        clears the flag, so the next breakage notifies again. Best-effort: a
        failure here is logged, never raised into the task.
        """
        if not self._oauth_credentials or self._mail_connector is None:
            logger.warning("[Worker] Gmail grant revoked but oauth_credentials not configured")
            return
        try:
            keys = await self._oauth_credentials.list_keys(user_id, self._mail_connector.kind)
            creds = await self._oauth_credentials.get(user_id, keys[0].credential_id) if keys else None
            if creds is None or creds.needs_reconnect:
                return
            # Before the save: a failure here must leave the flag down so the next run retries the notice.
            await self._ensure_agents(user_id)
            creds.needs_reconnect = True
            if not await self._oauth_credentials.update(creds):
                logger.info(f"[Worker] Gmail disconnected meanwhile for {user_id[:8]}, nothing to flag")
                return
            logger.warning(f"[Worker] Gmail marked needs_reconnect for {user_id[:8]}")
            await self._notification.notify(
                kind=NotificationKind.DOCUMENT_DELIVERY,
                user_id=user_id,
                account_id=account_id,
                system_alert=(
                    "Google revoked Alek's access to the user's Gmail (refresh token "
                    "rejected — usually a Google password change or access removed in "
                    "the Google account settings). Until the user reconnects, the daily "
                    "email review and email auto-indexing do not run. Tell them, and "
                    "that the fix is Cabinet → Gmail → Reconnect."
                ),
            )
        except Exception as exc:
            logger.error(f"[Worker] Gmail reconnect flag/notice failed for {user_id[:8]}: {exc}", exc_info=True)
