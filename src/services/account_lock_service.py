"""AccountLockService — what follows the account lock (CABINET_DEVICE_SESSIONS_RFC §7.8 steps 2–4).

The lock itself (step 1: `UserProfile.is_active = false` and `inactive_reason`) is written by the facts PIN transaction
(`FactsPinRepository.check`), on the duress code or the fifth wrong PIN in a row, so the flag and the counter are never
seen apart. `lock` then, in order:
2. revokes every device session and passkey of the user and cancels a pending PIN reset, in one transaction, and
3. revokes every MCP refresh token of the user: both are `DeviceSessionService.revoke_all` (reason `account_lock`),
   whose connector-grant step runs even when the session step raised;
4. posts the lock notice to every stored channel (primary and last active), the one notice that bypasses the inactive
   check of channel resolution (`UserNotificationService.notify_ui_notice_all_channels`).

Every step runs whatever the previous one did: a failure is logged at ERROR and the next step still runs. The account
stays locked in any case (the flag is already written); unlocking is an operator's hand edit in Firestore, never a
route, a command or a script. Facts and the account document are untouched.
"""
from typing import TYPE_CHECKING

from ..domain.device_session import SessionRevokeReason
from ..domain.ui_messages import UIMessage
from ..domain.user import InactiveReason
from ..utils.logger import logger

if TYPE_CHECKING:
    from .device_session_service import DeviceSessionService
    from .user_notification_service import UserNotificationService

_NOTICE = {
    InactiveReason.DURESS: UIMessage.ACCOUNT_LOCKED_DURESS_NOTICE,
    InactiveReason.PIN_ATTEMPTS: UIMessage.ACCOUNT_LOCKED_PIN_ATTEMPTS_NOTICE,
}


class AccountLockService:

    def __init__(self, device_sessions: "DeviceSessionService", notifications: "UserNotificationService") -> None:
        self._device_sessions = device_sessions
        self._notifications = notifications

    async def lock(self, user_id: str, reason: InactiveReason) -> None:
        """Steps 2–4 for a user whose flag the PIN transaction has just written."""
        logger.warning(f"🔒 Account lock: user={user_id[:8]} reason={reason.value}")
        try:
            revoked = await self._device_sessions.revoke_all(user_id, SessionRevokeReason.ACCOUNT_LOCK)
            logger.info(f"🔒 Account lock: user={user_id[:8]} sessions revoked={revoked}")
        except Exception as exc:
            logger.error(f"💥 Account lock: revoking the sessions or connector grants failed for {user_id[:8]}: {exc}",
                         exc_info=True)
        try:
            sent = await self._notifications.notify_ui_notice_all_channels(user_id, _NOTICE[reason])
        except Exception as exc:
            logger.error(f"💥 Account lock: the lock notice failed for {user_id[:8]}: {exc}", exc_info=True)
            return
        if sent == 0:
            logger.error(f"💥 Account lock: the lock notice found no channel for {user_id[:8]}; the Cabinet shows it")
