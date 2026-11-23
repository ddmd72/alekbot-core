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
- **Changing the set** (enrol, finish enrolling, remove) of a user who already has a live passkey needs a fresh
  passkey assertion on the caller's own session (`_require_fresh`, read from the store): otherwise a live session
  alone could remove every passkey, or add its own, and switch the call-start check off. Refused with
  `PasskeyFreshRequired`. A user without a live passkey enrols freely.
- **The call-start step-up** (§7.3, §13 decision 1): a voice call start of a user with a live passkey needs a passkey
  assertion on the session less than `DeviceSessionPolicy.CALL_PASSKEY_GRACE` old (`call_start_allowed`); a user
  without one starts as before. The step-up is a sign-in ceremony bound to the user and the session that began it: the
  assertion must come from a passkey of that user, and it is recorded on that session (`passkey_at`). A passkey
  sign-in counts too (`DeviceSessionService.start` sets `passkey_at`).

Every refusal raises `PasskeyRejected` and is logged without the nonce, the challenge or the response.
"""
import base64
import hashlib
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, NoReturn, Tuple

from ..domain.device_session import DeviceSession
from ..domain.device_session_policy import DeviceSessionPolicy
from ..domain.passkey import Passkey, PasskeyKind, credential_hash
from ..domain.passkey_ceremony import CeremonyStart, PasskeyFreshRequired, PasskeyRejected
from ..domain.ui_messages import UIMessage
from ..domain.user import UserProfile
from ..ports.device_session_repository import DeviceSessionRepository
from ..ports.ephemeral_store import EphemeralStore
from ..ports.signer_verifier import SignerVerifierPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService

_REGISTER, _SIGN_IN, _STEP_UP = "register", "sign_in", "step_up"


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
        await self._require_fresh(user.user_id, session)
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
        await self._require_fresh(user.user_id, session)
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
        passkey, sign_count = await self._verified(record, response)
        await self._touch(passkey, sign_count)
        logger.info(f"🔑 Passkey sign-in: user={passkey.user_id[:8]} key={passkey.cred_hash[:8]}")
        return passkey.user_id

    # ------------------------------------------------------------------ the call-start step-up

    async def live_passkeys(self, user_id: str) -> List[Passkey]:
        """The user's passkeys that are not revoked, oldest first."""
        return [p for p in await self._repo.list_passkeys(user_id) if p.revoked_at is None]

    async def call_start_allowed(self, user_id: str, sid: str) -> bool:
        """Whether session `sid` of `user_id` may start a voice call now: always for a user without a live passkey,
        otherwise only when a passkey assertion proved that session less than `CALL_PASSKEY_GRACE` ago. Read from
        the store, never from a cache: a store error propagates (the caller refuses)."""
        if not await self.live_passkeys(user_id):
            return True
        session = await self._repo.get(sid)
        if session is None or session.user_id != user_id:
            return False
        return DeviceSessionPolicy.passkey_fresh(session, self._clock())

    async def begin_step_up(self, user_id: str, sid: str) -> CeremonyStart:
        """Start the step-up of session `sid` (live, the caller's gate checked it) of `user_id`."""
        challenge, nonce = self._new_challenge(), self._new_nonce()
        options = await self._verifier.authentication_options(challenge=challenge)
        await self._challenges.set(_key(nonce), {"purpose": _STEP_UP, "challenge": _b64(challenge),
                                                 "user_id": user_id, "sid": sid}, self.CHALLENGE_TTL_S)
        return CeremonyStart(nonce=nonce, options=options)

    async def finish_step_up(self, nonce: str, response: Dict[str, Any], user_id: str, sid: str) -> None:
        """Verify the assertion for the step-up challenge `nonce` names, begun by `user_id` in session `sid`, from a
        passkey of that user; record the passkey's use and the assertion on the session."""
        record = await self._take(nonce, _STEP_UP)
        if record.get("user_id") != user_id or record.get("sid") != sid:
            _refuse(f"step-up challenge not begun by {user_id[:8]} in session {sid[:8]}")
        passkey, sign_count = await self._verified(record, response)
        if passkey.user_id != user_id:
            _refuse(f"credential {passkey.cred_hash[:8]} is not a passkey of {user_id[:8]}")
        await self._touch(passkey, sign_count)
        if not await self._repo.record_passkey_assertion(sid, user_id, self._clock()):
            _refuse(f"session {sid[:8]} could not record the assertion (absent, not the user's, or ended)")
        logger.info(f"🔑 Passkey step-up: user={user_id[:8]} sid={sid[:8]} key={passkey.cred_hash[:8]}")

    # ------------------------------------------------------------------ removal

    async def remove(self, user_id: str, cred_hash: str, session: DeviceSession) -> bool:
        """Revoke one passkey of `user_id` and tell the owner; `session` is the caller's own and must carry a fresh
        passkey assertion while the user has a live passkey (`PasskeyFreshRequired` otherwise). False for an unknown
        passkey, one of another user (refused, nothing written) or one already revoked."""
        await self._require_fresh(user_id, session)
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

    async def _require_fresh(self, user_id: str, session: DeviceSession) -> None:
        """Refuse unless the user has no live passkey or the caller's own session (re-read from the store, never the
        object the caller holds) was proved by a passkey assertion less than `CALL_PASSKEY_GRACE` ago."""
        if not await self.live_passkeys(user_id):
            return
        stored = await self._repo.get(session.sid)
        if (stored is not None and stored.user_id == user_id and stored.revoked_at is None
                and DeviceSessionPolicy.passkey_fresh(stored, self._clock())):
            return
        logger.warning(f"⛔ Passkey change refused: user={user_id[:8]} sid={session.sid[:8]} has no fresh assertion")
        raise PasskeyFreshRequired(f"session {session.sid[:8]} has no fresh passkey assertion")

    async def _take(self, nonce: str, purpose: str) -> Dict[str, Any]:
        """Consume the challenge record `nonce` names; refused when absent, expired, used, or of another ceremony."""
        record = await self._challenges.get_and_delete(_key(nonce))
        if record is None:
            _refuse(f"{purpose} challenge unknown, expired or already used")
        if record.get("purpose") != purpose:
            _refuse(f"{purpose} challenge is of another ceremony")
        return record

    async def _verified(self, record: Dict[str, Any], response: Dict[str, Any]) -> Tuple[Passkey, int]:
        """The live passkey an assertion names and the counter it reported, once the assertion verified against the
        record's challenge and the passkey's key, and its user handle is that passkey's user. Nothing is written."""
        cred_hash = credential_hash(await self._verifier.credential_id_of(response))
        passkey = await self._repo.get_passkey(cred_hash)
        if passkey is None or passkey.revoked_at is not None:
            _refuse(f"credential {cred_hash[:8]} is not a live passkey")
        verified = await self._verifier.verify_authentication(
            response, challenge=self._challenge_of(record), public_key=passkey.public_key,
            sign_count=passkey.sign_count)
        if verified.user_handle != passkey.user_id.encode("utf-8"):
            _refuse(f"credential {cred_hash[:8]} answered with another user handle than its passkey's")
        return passkey, verified.sign_count

    async def _touch(self, passkey: Passkey, sign_count: int) -> None:
        """Record the passkey's use; refused when it was revoked meanwhile or its counter did not advance."""
        if not await self._repo.touch_passkey(passkey.cred_hash, sign_count, self._clock()):
            _refuse(f"credential {passkey.cred_hash[:8]} use not recorded (revoked, or its counter did not advance)")

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
