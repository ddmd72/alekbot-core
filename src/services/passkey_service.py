"""PasskeyService — passkey enrolment and passkey sign-in for the Cabinet (CABINET_DEVICE_SESSIONS_RFC §7.3).

Each ceremony is two calls. `begin_*` makes a random challenge, stores it in the `EphemeralStore` for
`CHALLENGE_TTL_S` under the SHA-256 of a random nonce, and returns the nonce (the caller keeps it in a short-lived
httponly cookie) with the browser's options. `finish_*` consumes the stored challenge with `get_and_delete`, so an
unknown, expired or already used nonce is refused, and a challenge serves exactly one finish. The verifier
(`SignerVerifierPort`) checks the browser's response against it; the records live in `DeviceSessionRepository`, whose
`revoke_all` (the kill switch) revokes the passkeys with the sessions.

- **Enrolment** is done from a live session (the caller's gate checked it). Its challenge is bound to that user and
  sid, so a nonce begun in one session cannot finish in another. The new passkey takes the session's label and
  platform hint, and the owner is told ("Passkey added", sent literally: the label is untrusted).
- **Sign-in** uses a discoverable credential: no user is named first. The assertion names its credential; the stored
  passkey gives the key and the counter; the user handle the authenticator returns must be that passkey's user. The
  counter advance is written in one transaction (`touch_passkey`), so a replayed or cloned assertion racing the real
  one loses. It returns the user_id only: the caller runs `resume_session` and starts the device session.
- **Removal** revokes one passkey of the user and tells the owner.

Every refusal raises `PasskeyRejected` and is logged without the nonce, the challenge or the response.
"""
import base64
import hashlib
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, NoReturn

from ..domain.device_session import DeviceSession
from ..domain.passkey import Passkey, PasskeyKind, credential_hash
from ..domain.passkey_ceremony import CeremonyStart, PasskeyRejected
from ..domain.ui_messages import UIMessage
from ..domain.user import UserProfile
from ..ports.device_session_repository import DeviceSessionRepository
from ..ports.ephemeral_store import EphemeralStore
from ..ports.signer_verifier import SignerVerifierPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService

_REGISTER, _SIGN_IN = "register", "sign_in"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_nonce() -> str:
    return secrets.token_urlsafe(32)


def _new_challenge() -> bytes:
    return secrets.token_bytes(32)


def _key(nonce: str) -> str:
    return f"passkey_challenge:{hashlib.sha256(nonce.encode('utf-8')).hexdigest()}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _notice_time(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%H:%M UTC")


def _refuse(what: str) -> NoReturn:
    logger.warning(f"⛔ Passkey ceremony refused: {what}")
    raise PasskeyRejected(what)


class PasskeyService:

    CHALLENGE_TTL_S: ClassVar[int] = 300

    def __init__(self, repo: DeviceSessionRepository, verifier: SignerVerifierPort, challenges: EphemeralStore,
                 notifications: "UserNotificationService", *, clock: Callable[[], datetime] = _utcnow,
                 new_nonce: Callable[[], str] = _new_nonce, new_challenge: Callable[[], bytes] = _new_challenge) -> None:
        self._repo = repo
        self._verifier = verifier
        self._challenges = challenges
        self._notifications = notifications
        self._clock = clock
        self._new_nonce = new_nonce
        self._new_challenge = new_challenge

    # ------------------------------------------------------------------ enrolment

    async def begin_registration(self, user: UserProfile, session: DeviceSession) -> CeremonyStart:
        """Start adding a passkey from `session` (live, the caller's gate checked it) of `user`. The user's live
        passkeys are excluded, so the browser refuses to register one of them again."""
        if session.user_id != user.user_id:
            _refuse(f"session {session.sid[:8]} is not a session of {user.user_id[:8]}")
        live = [p.credential_id for p in await self._repo.list_passkeys(user.user_id) if p.revoked_at is None]
        challenge, nonce = self._new_challenge(), self._new_nonce()
        name = user.email or user.user_id
        options = await self._verifier.registration_options(
            user_id=user.user_id, user_name=name, display_name=user.display_name or name, challenge=challenge,
            exclude_credential_ids=live)
        await self._challenges.set(_key(nonce), {"purpose": _REGISTER, "challenge": _b64(challenge),
                                                 "user_id": user.user_id, "sid": session.sid}, self.CHALLENGE_TTL_S)
        return CeremonyStart(nonce=nonce, options=options)

    async def finish_registration(self, nonce: str, response: Dict[str, Any], user: UserProfile,
                                  session: DeviceSession) -> Passkey:
        """Verify the browser's new credential for the challenge `nonce` names, store the passkey and tell the owner.
        The challenge must have been begun by this user in this session."""
        record = await self._take(nonce, _REGISTER)
        if record.get("user_id") != user.user_id or record.get("sid") != session.sid or session.user_id != user.user_id:
            _refuse(f"registration challenge not begun by {user.user_id[:8]} in session {session.sid[:8]}")
        verified = await self._verifier.verify_registration(response, challenge=self._challenge_of(record))
        now = self._clock()
        passkey = Passkey(
            cred_hash=credential_hash(verified.credential_id), credential_id=verified.credential_id,
            user_id=user.user_id, account_id=session.account_id, kind=PasskeyKind.WEBAUTHN,
            public_key=verified.public_key, algorithm=verified.algorithm, sign_count=verified.sign_count,
            label=session.label, platform_hint=session.platform_hint, backup_eligible=verified.backup_eligible,
            created_at=now)
        if not await self._repo.add_passkey(passkey):
            _refuse(f"credential {passkey.cred_hash[:8]} is already registered")
        logger.info(f"🔑 Passkey enrolled: user={user.user_id[:8]} key={passkey.cred_hash[:8]} "
                    f"synced={passkey.backup_eligible}")
        await self._notify(passkey, UIMessage.PASSKEY_ADDED_NOTICE, now)
        return passkey

    # ------------------------------------------------------------------ sign-in

    async def begin_sign_in(self) -> CeremonyStart:
        """Start a sign-in with any passkey of this relying party (no user named)."""
        challenge, nonce = self._new_challenge(), self._new_nonce()
        options = await self._verifier.authentication_options(challenge=challenge)
        await self._challenges.set(_key(nonce), {"purpose": _SIGN_IN, "challenge": _b64(challenge)},
                                   self.CHALLENGE_TTL_S)
        return CeremonyStart(nonce=nonce, options=options)

    async def finish_sign_in(self, nonce: str, response: Dict[str, Any]) -> str:
        """Verify the browser's assertion for the challenge `nonce` names and record the passkey's use. Returns the
        passkey's user_id; the caller still runs `resume_session` (active user, whitelist, account) before a session."""
        record = await self._take(nonce, _SIGN_IN)
        cred_hash = credential_hash(await self._verifier.credential_id_of(response))
        passkey = await self._repo.get_passkey(cred_hash)
        if passkey is None or passkey.revoked_at is not None:
            _refuse(f"credential {cred_hash[:8]} is not a live passkey")
        verified = await self._verifier.verify_authentication(
            response, challenge=self._challenge_of(record), public_key=passkey.public_key,
            sign_count=passkey.sign_count)
        if verified.user_handle != passkey.user_id.encode("utf-8"):
            _refuse(f"credential {cred_hash[:8]} answered with another user handle than its passkey's")
        if not await self._repo.touch_passkey(cred_hash, verified.sign_count, self._clock()):
            _refuse(f"credential {cred_hash[:8]} use not recorded (revoked, or its counter did not advance)")
        logger.info(f"🔑 Passkey sign-in: user={passkey.user_id[:8]} key={cred_hash[:8]}")
        return passkey.user_id

    # ------------------------------------------------------------------ removal

    async def remove(self, user_id: str, cred_hash: str) -> bool:
        """Revoke one passkey of `user_id` and tell the owner. False for an unknown passkey, one of another user
        (refused, nothing written) or one already revoked."""
        passkey = await self._repo.get_passkey(cred_hash)
        if passkey is None or passkey.user_id != user_id:
            logger.warning(f"⛔ Passkey removal refused: key={cred_hash[:8]} is not a passkey of {user_id[:8]}")
            return False
        now = self._clock()
        if not await self._repo.revoke_passkey(cred_hash, now):
            return False
        await self._notify(passkey, UIMessage.PASSKEY_REMOVED_NOTICE, now)
        return True

    # ------------------------------------------------------------------ helpers

    async def _take(self, nonce: str, purpose: str) -> Dict[str, Any]:
        """Consume the challenge record `nonce` names; refused when absent, expired, used, or of another ceremony."""
        record = await self._challenges.get_and_delete(_key(nonce))
        if record is None:
            _refuse(f"{purpose} challenge unknown, expired or already used")
        if record.get("purpose") != purpose:
            _refuse(f"{purpose} challenge is of another ceremony")
        return record

    @staticmethod
    def _challenge_of(record: Dict[str, Any]) -> bytes:
        return base64.urlsafe_b64decode(record["challenge"])

    async def _notify(self, passkey: Passkey, message: UIMessage, now: datetime) -> None:
        """A passkey notice; a failure is logged and never fails the ceremony."""
        try:
            sent = await self._notifications.notify_ui_notice(
                passkey.user_id, passkey.account_id, message, label=passkey.label, time=_notice_time(now))
        except Exception as exc:
            logger.error(f"💥 Passkey notice failed: user={passkey.user_id[:8]} key={passkey.cred_hash[:8]}: {exc}",
                         exc_info=True)
            return
        if not sent:
            logger.warning(f"Passkey notice not delivered: user={passkey.user_id[:8]} key={passkey.cred_hash[:8]}")
