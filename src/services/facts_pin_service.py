"""FactsPinService — the opt-in facts PIN and the duress code (CABINET_DEVICE_SESSIONS_RFC §7.7, §7.8, §7.9).

A user who never set a PIN has no record: `facts_open` is always true for them, `check` answers `NO_PIN` and counts
nothing, and nothing locks them through the PIN. For a user who set one:

- **Codes** are kept as keyed digests (`keyed_digest(SECRET_DIGEST_KEY, "facts_pin" | "duress_pin",
  f"{user_id}:{code}")`); one attempt digests the code both ways and the store compares with `hmac.compare_digest`
  (`FactsPinPolicy.decide`). A code is never logged, never stored, never echoed: logs carry the user's id prefix, the
  surface and the outcome only.
- **`check`** runs the store's one transaction (counter, and the account flag on the duress code or the fifth wrong code
  in a row); when that transaction wrote the lock, `AccountLockService.lock` runs the rest of the lock.
- **`set`** stores a first PIN and duress code; **`change`** and **`remove`** need the current PIN, checked as an attempt
  (a wrong one counts, the duress code locks), and removing sends a chat notice.
- **`unlock_session`** checks the PIN and, when right, opens the facts tab on that session for `UNLOCK_TTL`;
  `relock_session` closes it early; `facts_open` is the read the facts routes' guard asks.
- **A forgotten PIN**: `request_reset` needs a Google sign-in under `RESET_SIGN_IN_MAX_AGE` on the session; the reset is
  pending for `RESET_DELAY` with a chat notice ("Not you? `$pin cancel`"), `cancel_reset` ends it, and once due it is
  applied at the next `check` or `facts_open` (the record is deleted, a second notice follows): the user then sets a new
  PIN, or none. The kill switch and the account lock cancel a pending reset (`DeviceSessionRepository.revoke_all`).

Notices go through `UserNotificationService.notify_ui_notice` (sent literally: the platform hint is untrusted text); a
failed notice is logged and never fails the operation.
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional, Tuple

from ..domain.device_session import DeviceSession
from ..domain.device_session_policy import DeviceSessionPolicy
from ..domain.facts_pin import FactsPin
from ..domain.facts_pin_policy import (
    FactsPinPolicy, PinChange, PinChangeStatus, PinOutcome, PinSurface, PinVerdict,
)
from ..domain.secret_digest import keyed_digest
from ..domain.ui_messages import UIMessage
from ..ports.device_session_repository import DeviceSessionRepository
from ..ports.facts_pin_repository import FactsPinRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .account_lock_service import AccountLockService
    from .user_notification_service import UserNotificationService

_PIN_PURPOSE, _DURESS_PURPOSE = "facts_pin", "duress_pin"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _notice_time(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class FactsPinService:

    def __init__(self, repo: FactsPinRepository, sessions: DeviceSessionRepository, account_lock: "AccountLockService",
                 notifications: "UserNotificationService", digest_key: bytes, *,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._repo = repo
        self._sessions = sessions
        self._lock = account_lock
        self._notifications = notifications
        self._digest_key = digest_key
        self._clock = clock

    # ------------------------------------------------------------------ an attempt

    async def check(self, user_id: str, account_id: str, code: str, surface: PinSurface) -> PinOutcome:
        """One attempt with `code` from `surface`. A due reset is applied first, so a reset PIN never counts. On the
        duress code or the fifth wrong code in a row the account is locked (the flag in the store's transaction, the
        rest by `AccountLockService.lock`)."""
        now = self._clock()
        await self._apply_due_reset(user_id, account_id, now)
        outcome = await self._repo.check(user_id, self._digest(_PIN_PURPOSE, user_id, code),
                                         self._digest(_DURESS_PURPOSE, user_id, code), now)
        logger.info(f"🔢 Facts PIN attempt: user={user_id[:8]} surface={surface.value} "
                    f"outcome={outcome.verdict.value} left={outcome.attempts_left}")
        reason = outcome.lock_reason
        if reason is not None:
            await self._lock.lock(user_id, reason)
        return outcome

    # ------------------------------------------------------------------ setting, changing, removing

    async def set(self, user_id: str, pin: str, duress: str) -> PinChange:
        """Set a first PIN and duress code. Refused when a code breaks a rule or the user already has a PIN."""
        problem = FactsPinPolicy.code_problem(pin, duress)
        if problem is not None:
            return PinChange(PinChangeStatus.CODES_REFUSED, problem=problem)
        if not await self._repo.set_codes(user_id, self._digest(_PIN_PURPOSE, user_id, pin),
                                          self._digest(_DURESS_PURPOSE, user_id, duress), self._clock(),
                                          replace=False):
            return PinChange(PinChangeStatus.ALREADY_SET)
        return PinChange(PinChangeStatus.DONE)

    async def change(self, user_id: str, account_id: str, current: str, pin: str, duress: str) -> PinChange:
        """Replace both codes. The new codes are checked against the rules first (nothing counted when they break
        one), then `current` as a PIN attempt (`check`): only `OK` lets the change through."""
        problem = FactsPinPolicy.code_problem(pin, duress)
        if problem is not None:
            return PinChange(PinChangeStatus.CODES_REFUSED, problem=problem)
        refused = await self._verify(user_id, account_id, current)
        if refused is not None:
            return refused
        if not await self._repo.set_codes(user_id, self._digest(_PIN_PURPOSE, user_id, pin),
                                          self._digest(_DURESS_PURPOSE, user_id, duress), self._clock(),
                                          replace=True):
            return PinChange(PinChangeStatus.NO_PIN)
        return PinChange(PinChangeStatus.DONE)

    async def remove(self, user_id: str, account_id: str, current: str) -> PinChange:
        """Remove the PIN (the facts tab opens without one again), with `current` checked as a PIN attempt first, and
        tell the owner in chat."""
        refused = await self._verify(user_id, account_id, current)
        if refused is not None:
            return refused
        if not await self._repo.clear(user_id):
            return PinChange(PinChangeStatus.NO_PIN)
        await self._notify(user_id, account_id, UIMessage.FACTS_PIN_REMOVED_NOTICE, time=_notice_time(self._clock()))
        return PinChange(PinChangeStatus.DONE)

    # ------------------------------------------------------------------ the session's unlock

    async def unlock_session(self, user_id: str, account_id: str, sid: str, code: str) -> Tuple[PinOutcome, bool]:
        """Check `code` from the Cabinet and, when it is the PIN, open the facts tab on session `sid` until
        `FactsPinPolicy.unlock_end`. Returns the attempt and whether the session is now unlocked. A sid that is not a
        live session of `user_id` raises `PermissionError` before anything is counted."""
        await self._own_live_session(user_id, sid)
        outcome = await self.check(user_id, account_id, code, PinSurface.CABINET)
        if outcome.verdict is not PinVerdict.OK:
            return outcome, False
        now = self._clock()
        return outcome, await self._sessions.set_facts_unlock(sid, FactsPinPolicy.unlock_end(now), now)

    async def relock_session(self, user_id: str, sid: str) -> bool:
        """Close the facts tab on session `sid` of `user_id` before its unlock ends. False for a sid that is not a live
        session of the user (nothing written)."""
        try:
            await self._own_live_session(user_id, sid)
        except PermissionError:
            return False
        return await self._sessions.set_facts_unlock(sid, None, self._clock())

    async def facts_open(self, user_id: str, account_id: str, sid: str) -> bool:
        """Whether the facts routes may answer on session `sid`: always for a user without a PIN (a due reset is
        applied first), otherwise only while that live session of the user is unlocked."""
        now = self._clock()
        await self._apply_due_reset(user_id, account_id, now)
        if await self._repo.get(user_id) is None:
            return True
        session = await self._sessions.get(sid)
        return session is not None and session.user_id == user_id and FactsPinPolicy.is_unlocked(session, now)

    # ------------------------------------------------------------------ a forgotten PIN

    async def status(self, user_id: str, account_id: str) -> Optional[FactsPin]:
        """The user's PIN record for the Cabinet's settings card and facts tab (None: no PIN). A due reset is applied
        first, so a reset PIN reads as none. The record holds digests: the caller reads flags and dates from it only."""
        await self._apply_due_reset(user_id, account_id, self._clock())
        return await self._repo.get(user_id)

    def can_request_reset(self, session: DeviceSession) -> bool:
        """Whether `request_reset` would be accepted from `session` now (its Google sign-in is fresh and it is live)."""
        return FactsPinPolicy.may_request_reset(session, self._clock())

    async def request_reset(self, session: DeviceSession) -> Optional[datetime]:
        """Ask for a reset from `session` (the caller's gate checked it). Returns when it takes effect: a new request's
        due time (and the owner is told), or the pending one's (nothing sent again). None when refused: the session is
        not live or its Google sign-in is not fresh, or the user has no PIN."""
        now = self._clock()
        if not FactsPinPolicy.may_request_reset(session, now):
            logger.warning(f"⛔ Facts PIN reset refused: session {session.sid[:8]} has no Google sign-in under "
                           f"{FactsPinPolicy.RESET_SIGN_IN_MAX_AGE}")
            return None
        if await self._repo.request_reset(session.user_id, now):
            due = FactsPinPolicy.reset_due(now)
            await self._notify(session.user_id, session.account_id, UIMessage.FACTS_PIN_RESET_REQUESTED_NOTICE,
                               platform_hint=session.platform_hint, time=_notice_time(due))
            return due
        record = await self._repo.get(session.user_id)
        return None if record is None else record.reset_due_at

    async def cancel_reset(self, user_id: str) -> bool:
        """End a pending reset (`$pin cancel`). False when none is pending."""
        return await self._repo.cancel_reset(user_id, self._clock())

    # ------------------------------------------------------------------ helpers

    def _digest(self, purpose: str, user_id: str, code: str) -> str:
        return keyed_digest(self._digest_key, purpose, f"{user_id}:{code}")

    async def _verify(self, user_id: str, account_id: str, current: str) -> Optional[PinChange]:
        """None when `current` is the PIN; the refusal otherwise (a user without a PIN, or the attempt that failed)."""
        attempt = await self.check(user_id, account_id, current, PinSurface.CABINET)
        if attempt.verdict is PinVerdict.OK:
            return None
        if attempt.verdict is PinVerdict.NO_PIN:
            return PinChange(PinChangeStatus.NO_PIN)
        return PinChange(PinChangeStatus.NOT_VERIFIED, attempt=attempt)

    async def _own_live_session(self, user_id: str, sid: str) -> None:
        session = await self._sessions.get(sid)
        if session is None or session.user_id != user_id or not DeviceSessionPolicy.is_live(session, self._clock()):
            logger.warning(f"⛔ Facts PIN: sid={sid[:8]} is not a live session of {user_id[:8]}")
            raise PermissionError("not a live session of this user")

    async def _apply_due_reset(self, user_id: str, account_id: str, now: datetime) -> None:
        if await self._repo.apply_reset(user_id, now):
            await self._notify(user_id, account_id, UIMessage.FACTS_PIN_RESET_DONE_NOTICE, time=_notice_time(now))

    async def _notify(self, user_id: str, account_id: str, message: UIMessage, **fields: str) -> None:
        """A PIN notice; a failure is logged and never fails the operation."""
        try:
            sent = await self._notifications.notify_ui_notice(user_id, account_id, message, **fields)
        except Exception as exc:
            logger.error(f"💥 Facts PIN notice {message.value} failed: user={user_id[:8]}: {exc}", exc_info=True)
            return
        if not sent:
            logger.warning(f"Facts PIN notice {message.value} not delivered: user={user_id[:8]}")
