"""PasskeyService (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.6) end to end below the routes: the real WebAuthn verifier for
`cabinet.example.com` answered by a software authenticator (`tests/support/webauthn_authenticator.py`), the in-memory
repository twin, the real `FirestoreEphemeralStore` over `EvalDb` for the challenges, and the real
`UserNotificationService` posting to recording channels, with a controlled clock.

Covers: enrolment (the stored passkey takes the session's label, the literal "Passkey added" notice, the user's live
passkeys excluded, a credential registered twice refused, a failed notice never failing it), the challenge (single
use, expired after `CHALLENGE_TTL_S`, bound to its user, its session and its ceremony), discoverable sign-in (the
user_id, the counter and last use recorded, every refusal: unknown, revoked, another user handle, none, a counter that
did not advance, two racing assertions), removal (its notice, another user's passkey untouched) and the kill switch
revoking passkeys with the sessions."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters import firestore_ephemeral_store
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.adapters.webauthn_signer_verifier import WebAuthnSignerVerifier
from src.domain.billing import AccountTier, BillingAccount
from src.domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy as P
from src.domain.passkey import Passkey, PasskeyKind, credential_hash
from src.domain.passkey_ceremony import PasskeyRejected
from src.domain.user import UserProfile
from src.services.device_session_service import DeviceSessionService
from src.services.localization_service import LocalizationService
from src.services.passkey_service import PasskeyService
from src.services.session_service import SessionService
from src.services.user_notification_service import UserNotificationService
from tests.support.clock_instants import pin_datetime
from tests.support.device_session_repo import FakeDeviceSessionRepository
from tests.support.fact_review_world import FakeNotificationState, RecordingChannelFactory
from tests.support.firestore_eval import EvalDb
from tests.support.webauthn_authenticator import SoftAuthenticator, b64u

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
RP, ORIGIN = "cabinet.example.com", "https://cabinet.example.com"
CHALLENGES = "t_auth_passkey_challenges"
PHONE, MAC = "iPhone — Home Screen (call)", "Mac — Chrome"
ALEX = UserProfile(user_id="user-alex", external_user_id="firebase|alex", email="alex@example.com",
                   display_name="Alex", account_id="acct-alex")
OTHER = UserProfile(user_id="user-other", external_user_id="firebase|other", email="other@example.com",
                    display_name="Other", account_id="acct-other")


def device(sid: str, user: UserProfile = ALEX, hint: str = PHONE) -> DeviceSession:
    return DeviceSession(sid=sid, user_id=user.user_id, account_id=user.account_id or "", label=hint,
                         platform_hint=hint, auth_kind=SessionAuthKind.GOOGLE, auth_at=T0, created_at=T0,
                         last_seen_at=T0, expires_at=P.absolute_end(T0), refresh_hash="h", purge_after=P.purge_after(T0))


PHONE_SESSION, MAC_SESSION, OTHER_SESSION = device("sid-phone"), device("sid-mac", hint=MAC), device("sid-o", OTHER)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def counter(prefix: str):
    n = iter(range(1, 1000))
    return lambda: f"{prefix}-{next(n)}"


def challenges():
    n = iter(range(1, 1000))
    return lambda: f"challenge-{next(n):03d}".encode().ljust(32, b".")


class World:
    def __init__(self, notifications=None, authenticator=None) -> None:
        self.clock = Clock()
        self.repo = FakeDeviceSessionRepository()
        self.db = EvalDb()
        self.challenges = FirestoreEphemeralStore(self.db, CHALLENGES)
        self.state = FakeNotificationState(T0)
        self.channels = RecordingChannelFactory()
        self.notifier = notifications or UserNotificationService(
            self.state, self.channels, coordinator=None, notification_sla={},
            localization=LocalizationService(FileLocalizationAdapter()))
        self.verifier = WebAuthnSignerVerifier(rp_id=RP, rp_name="Alek", origin=ORIGIN)
        self.service = PasskeyService(self.repo, self.verifier, self.challenges, self.notifier, clock=self.clock,
                                      new_nonce=counter("nonce"), new_challenge=challenges())
        self.authenticator = authenticator or SoftAuthenticator(RP, ORIGIN)

    async def enrol(self, session: DeviceSession = PHONE_SESSION, user: UserProfile = ALEX, **register) -> Passkey:
        start = await self.service.begin_registration(user, session)
        return await self.service.finish_registration(
            start.nonce, self.authenticator.register(start.options, **register), user, session)

    async def sign_in(self, **assertion) -> str:
        start = await self.service.begin_sign_in()
        return await self.service.finish_sign_in(start.nonce, self.authenticator.sign_in(start.options, **assertion))


@pytest.fixture
async def world():
    w = World()
    await w.state.save("user-alex", "telegram", "chat-alex")
    return w


# ---------------------------------------------------------------------------------------------- enrolment

class TestEnrolment:
    async def test_enrolment_stores_the_passkey_with_the_sessions_label(self, world):
        stored = await world.enrol()
        [cred] = world.authenticator.credentials
        assert world.repo.passkeys == {stored.cred_hash: stored}
        assert stored == Passkey(
            cred_hash=credential_hash(cred.credential_id), credential_id=cred.credential_id, user_id="user-alex",
            account_id="acct-alex", kind=PasskeyKind.WEBAUTHN, public_key=cred.cose_public_key(), algorithm=-7,
            sign_count=0, label=PHONE, platform_hint=PHONE, backup_eligible=False, created_at=T0)

    async def test_a_synced_passkey_is_stored_backup_eligible(self):
        world = World(authenticator=SoftAuthenticator(RP, ORIGIN, backup_eligible=True, counter_step=0))
        assert (await world.enrol()).backup_eligible is True

    async def test_the_options_name_the_user_and_exclude_only_live_passkeys(self, world):
        first = await world.enrol()
        second = await world.enrol(MAC_SESSION)
        await world.service.remove("user-alex", first.cred_hash)
        start = await world.service.begin_registration(ALEX, PHONE_SESSION)
        assert start.options["user"] == {"id": b64u(b"user-alex"), "name": "alex@example.com", "displayName": "Alex"}
        assert start.options["excludeCredentials"] == [{"id": b64u(second.credential_id), "type": "public-key"}]
        assert start.options["challenge"] == b64u(b"challenge-003".ljust(32, b"."))
        assert start.nonce == "nonce-3"

    async def test_a_user_without_email_or_name_is_named_by_user_id(self, world):
        bare = UserProfile(user_id="user-bare", display_name="", account_id="acct-alex")
        start = await world.service.begin_registration(bare, device("sid-bare", bare))
        assert start.options["user"] == {"id": b64u(b"user-bare"), "name": "user-bare", "displayName": "user-bare"}

    async def test_the_added_notice_carries_the_label_literally(self, world):
        hostile = "Mac — Chrome <!channel> {time} *bold*"
        await world.enrol(device("sid-h", hint=hostile))
        [(platform, channel, text)] = world.channels.sent_literal
        assert (platform, channel) == ("telegram", "chat-alex")
        assert hostile in text and "09:00 UTC" in text and "$devices off" in text
        assert world.channels.sent == world.channels.sent_literal

    async def test_a_session_of_another_user_cannot_begin(self, world):
        with pytest.raises(PasskeyRejected, match="not a session of"):
            await world.service.begin_registration(ALEX, OTHER_SESSION)
        assert world.db.collection(CHALLENGES).docs == {}

    async def test_a_credential_registered_twice_is_refused(self, world):
        await world.enrol()
        [cred] = world.authenticator.credentials
        with pytest.raises(PasskeyRejected, match="already registered"):
            await world.enrol(MAC_SESSION, reuse=cred)
        assert [k.label for k in world.repo.passkeys.values()] == [PHONE]
        assert len(world.channels.sent) == 1

    async def test_a_registration_that_does_not_verify_stores_nothing(self, world):
        with pytest.raises(PasskeyRejected, match="User verification is required"):
            await world.enrol(user_verified=False)
        assert world.repo.passkeys == {} and world.channels.sent == []

    async def test_an_undelivered_notice_never_fails_the_enrolment(self, world, caplog):
        world.state.last.clear()
        stored = await world.enrol()
        assert world.repo.passkeys == {stored.cred_hash: stored}
        assert "Passkey notice not delivered" in caplog.text

    async def test_a_raising_notice_never_fails_the_enrolment(self, caplog):
        class Broken:
            async def notify_ui_notice(self, *args, **fields):
                raise RuntimeError("channel down")

        world = World(notifications=Broken())
        stored = await world.enrol()
        assert world.repo.passkeys == {stored.cred_hash: stored}
        assert "Passkey notice failed" in caplog.text


# ---------------------------------------------------------------------------------------------- the challenge

class TestChallenge:
    async def test_a_challenge_serves_one_finish(self, world):
        start = await world.service.begin_registration(ALEX, PHONE_SESSION)
        response = world.authenticator.register(start.options)
        await world.service.finish_registration(start.nonce, response, ALEX, PHONE_SESSION)
        with pytest.raises(PasskeyRejected, match="unknown, expired or already used"):
            await world.service.finish_registration(start.nonce, response, ALEX, PHONE_SESSION)
        assert len(world.repo.passkeys) == 1

    async def test_the_stored_record_holds_a_digest_of_the_nonce_never_the_nonce(self, world):
        start = await world.service.begin_sign_in()
        [key] = world.db.collection(CHALLENGES).docs
        assert key.startswith("passkey_challenge:") and start.nonce not in key
        assert start.nonce not in str(world.db.collection(CHALLENGES).docs[key])

    @pytest.mark.parametrize("ceremony", ["registration", "sign_in"])
    async def test_an_expired_challenge_is_refused(self, world, monkeypatch, ceremony):
        pin_datetime(monkeypatch, firestore_ephemeral_store, T0)
        if ceremony == "registration":
            start = await world.service.begin_registration(ALEX, PHONE_SESSION)
            response = world.authenticator.register(start.options)
            finish = world.service.finish_registration(start.nonce, response, ALEX, PHONE_SESSION)
        else:
            await world.enrol()
            start = await world.service.begin_sign_in()
            finish = world.service.finish_sign_in(start.nonce, world.authenticator.sign_in(start.options))
        pin_datetime(monkeypatch, firestore_ephemeral_store, T0 + timedelta(seconds=PasskeyService.CHALLENGE_TTL_S + 1))
        with pytest.raises(PasskeyRejected, match="unknown, expired or already used"):
            await finish

    async def test_the_ttl_is_five_minutes(self):
        assert PasskeyService.CHALLENGE_TTL_S == 300

    @pytest.mark.parametrize("user, session", [(ALEX, MAC_SESSION), (OTHER, OTHER_SESSION)],
                             ids=["another-session", "another-user"])
    async def test_a_registration_finishes_only_in_the_session_that_began_it(self, world, user, session):
        start = await world.service.begin_registration(ALEX, PHONE_SESSION)
        response = world.authenticator.register(start.options)
        with pytest.raises(PasskeyRejected, match="not begun by"):
            await world.service.finish_registration(start.nonce, response, user, session)
        assert world.repo.passkeys == {}

    async def test_a_challenge_of_one_ceremony_cannot_finish_the_other(self, world):
        sign_in = await world.service.begin_sign_in()
        with pytest.raises(PasskeyRejected, match="of another ceremony"):
            await world.service.finish_registration(sign_in.nonce, {}, ALEX, PHONE_SESSION)
        enrol = await world.service.begin_registration(ALEX, PHONE_SESSION)
        with pytest.raises(PasskeyRejected, match="of another ceremony"):
            await world.service.finish_sign_in(enrol.nonce, {})

    async def test_an_unknown_nonce_is_refused(self, world):
        with pytest.raises(PasskeyRejected, match="unknown"):
            await world.service.finish_sign_in("nonce-forged", {})


# ---------------------------------------------------------------------------------------------- sign-in

class TestSignIn:
    async def test_a_sign_in_returns_the_user_and_records_the_use(self, world):
        stored = await world.enrol()
        world.clock.now = T0 + timedelta(days=2)
        assert await world.sign_in() == "user-alex"
        assert world.repo.passkeys[stored.cred_hash] == stored.model_copy(
            update={"sign_count": 1, "last_used_at": T0 + timedelta(days=2)})

    async def test_the_options_name_no_credential(self, world):
        start = await world.service.begin_sign_in()
        assert (start.options["rpId"], start.options["allowCredentials"], start.options["userVerification"]) == (
            RP, [], "required")

    async def test_a_synced_passkey_signs_in_again_and_again_at_counter_zero(self):
        world = World(authenticator=SoftAuthenticator(RP, ORIGIN, backup_eligible=True, counter_step=0))
        stored = await world.enrol()
        assert [await world.sign_in(), await world.sign_in()] == ["user-alex", "user-alex"]
        assert world.repo.passkeys[stored.cred_hash].sign_count == 0

    async def test_an_unknown_credential_is_refused(self, world):
        world.authenticator.register((await world.service.begin_registration(ALEX, PHONE_SESSION)).options)
        with pytest.raises(PasskeyRejected, match="is not a live passkey"):
            await world.sign_in()                        # registered at the authenticator, never finished here

    async def test_a_removed_passkey_no_longer_signs_in(self, world):
        stored = await world.enrol()
        assert await world.service.remove("user-alex", stored.cred_hash) is True
        with pytest.raises(PasskeyRejected, match="is not a live passkey"):
            await world.sign_in()

    async def test_the_kill_switch_revokes_the_passkeys_with_the_sessions(self, world):
        stored = await world.enrol()
        sessions = DeviceSessionService(world.repo, SessionService(secret_key="k" * 40), world.notifier,
                                        clock=world.clock)
        await world.repo.create(PHONE_SESSION)
        assert await sessions.revoke_all("user-alex", SessionRevokeReason.KILL_SWITCH) == 1
        assert world.repo.passkeys[stored.cred_hash].revoked_at == T0
        with pytest.raises(PasskeyRejected, match="is not a live passkey"):
            await world.sign_in()

    @pytest.mark.parametrize("assertion, match", [
        (dict(user_handle=b"user-other"), "another user handle"),
        (dict(user_handle=None), "another user handle"),
        (dict(user_verified=False), "User verification is required"),
    ], ids=["another-user-handle", "no-user-handle", "no-user-verification"])
    async def test_an_assertion_that_does_not_hold_records_nothing(self, world, assertion, match):
        stored = await world.enrol()
        with pytest.raises(PasskeyRejected, match=match):
            await world.sign_in(**assertion)
        assert world.repo.passkeys[stored.cred_hash] == stored

    async def test_a_counter_that_went_back_is_refused(self, world):
        stored = await world.enrol()
        await world.sign_in()
        await world.sign_in()
        with pytest.raises(PasskeyRejected, match="sign count"):
            await world.sign_in(counter=1)              # a clone that stayed at the first use
        assert world.repo.passkeys[stored.cred_hash].sign_count == 2

    async def test_of_two_racing_assertions_with_one_counter_only_one_signs_in(self, world):
        stored = await world.enrol()
        first, second = await world.service.begin_sign_in(), await world.service.begin_sign_in()
        one = world.authenticator.sign_in(first.options, counter=1)
        two = world.authenticator.sign_in(second.options, counter=1)
        both_read = asyncio.Barrier(2)
        read = world.repo.get_passkey

        async def get_passkey(cred_hash):                # both finishes read counter 0 before either writes
            found = await read(cred_hash)
            await asyncio.wait_for(both_read.wait(), timeout=2)
            return found

        world.repo.get_passkey = get_passkey
        results = await asyncio.gather(world.service.finish_sign_in(first.nonce, one),
                                       world.service.finish_sign_in(second.nonce, two), return_exceptions=True)
        assert sorted(type(r).__name__ for r in results) == ["PasskeyRejected", "str"]
        assert "counter did not advance" in str(next(r for r in results if isinstance(r, PasskeyRejected)))
        assert world.repo.passkeys[stored.cred_hash].sign_count == 1


# ---------------------------------------------------------------------------------------------- removal

class TestRemove:
    async def test_remove_revokes_and_tells_the_owner(self, world):
        stored = await world.enrol()
        world.clock.now = T0 + timedelta(hours=3)
        assert await world.service.remove("user-alex", stored.cred_hash) is True
        assert world.repo.passkeys[stored.cred_hash].revoked_at == T0 + timedelta(hours=3)
        added, removed = [text for _, _, text in world.channels.sent_literal]
        assert PHONE in removed and "12:00 UTC" in removed and removed != added

    async def test_a_passkey_of_another_user_is_untouched(self, world):
        stored = await world.enrol()
        assert await world.service.remove("user-other", stored.cred_hash) is False
        assert world.repo.passkeys[stored.cred_hash] == stored
        assert await world.service.remove("user-alex", "key-unknown") is False
        assert len(world.channels.sent) == 1

    async def test_removing_twice_tells_the_owner_once(self, world):
        stored = await world.enrol()
        assert await world.service.remove("user-alex", stored.cred_hash) is True
        assert await world.service.remove("user-alex", stored.cred_hash) is False
        assert len(world.channels.sent) == 2


# ---------------------------------------------------------------------------------------------- defaults

# ---------------------------------------------------------------------------------------------- call-start step-up

class TestCallStartStepUp:
    """§7.3, §13 decision 1: a call start of a user with a live passkey needs an assertion on the session under five
    minutes old; the step-up ceremony gives one, bound to the user and the session that began it."""

    async def step_up(self, world, sid="sid-phone", user_id="user-alex", **assertion):
        start = await world.service.begin_step_up(user_id, sid)
        await world.service.finish_step_up(start.nonce, world.authenticator.sign_in(start.options, **assertion),
                                           user_id, sid)

    async def test_a_user_without_a_live_passkey_starts_a_call_as_before(self, world):
        assert await world.service.call_start_allowed("user-alex", "sid-unknown") is True
        stored = await world.enrol()
        await world.service.remove("user-alex", stored.cred_hash)
        assert await world.service.live_passkeys("user-alex") == []
        assert await world.service.call_start_allowed("user-alex", "sid-unknown") is True

    async def test_with_a_passkey_a_session_never_proved_by_one_may_not_start(self, world):
        await world.repo.create(PHONE_SESSION)
        await world.enrol()
        assert await world.service.call_start_allowed("user-alex", "sid-phone") is False

    @pytest.mark.parametrize("sid", ["sid-unknown", "sid-o"], ids=["unknown-session", "another-users-session"])
    async def test_with_a_passkey_a_session_that_is_not_the_users_may_not_start(self, world, sid):
        await world.repo.create(OTHER_SESSION)
        await world.repo.record_passkey_assertion("sid-o", "user-other", T0)
        await world.enrol()
        assert await world.service.call_start_allowed("user-alex", sid) is False

    async def test_a_step_up_records_the_assertion_and_the_use_and_lasts_five_minutes(self, world):
        await world.repo.create(PHONE_SESSION)
        stored = await world.enrol()
        world.clock.now = T0 + timedelta(hours=1)
        await self.step_up(world)
        assert world.repo.sessions["sid-phone"].passkey_at == T0 + timedelta(hours=1)
        assert world.repo.passkeys[stored.cred_hash].last_used_at == T0 + timedelta(hours=1)
        world.clock.now = T0 + timedelta(hours=1, minutes=4, seconds=59)
        assert await world.service.call_start_allowed("user-alex", "sid-phone") is True    # a redial after a drop
        world.clock.now = T0 + timedelta(hours=1, minutes=5)
        assert await world.service.call_start_allowed("user-alex", "sid-phone") is False

    async def test_the_assertion_proves_only_its_own_session(self, world):
        await world.repo.create(PHONE_SESSION)
        await world.repo.create(MAC_SESSION)
        await world.enrol()
        await self.step_up(world, sid="sid-phone")
        assert await world.service.call_start_allowed("user-alex", "sid-mac") is False

    async def test_the_options_name_no_credential_and_require_user_verification(self, world):
        start = await world.service.begin_step_up("user-alex", "sid-phone")
        assert (start.options["rpId"], start.options["allowCredentials"], start.options["userVerification"]) == (
            RP, [], "required")

    @pytest.mark.parametrize("user_id, sid", [("user-alex", "sid-mac"), ("user-other", "sid-phone")],
                             ids=["another-session", "another-user"])
    async def test_a_step_up_finishes_only_where_it_began(self, world, user_id, sid):
        await world.repo.create(PHONE_SESSION)
        stored = await world.enrol()
        start = await world.service.begin_step_up("user-alex", "sid-phone")
        with pytest.raises(PasskeyRejected, match="step-up challenge not begun by"):
            await world.service.finish_step_up(start.nonce, world.authenticator.sign_in(start.options), user_id, sid)
        assert world.repo.sessions["sid-phone"].passkey_at is None
        assert world.repo.passkeys[stored.cred_hash] == stored

    async def test_a_passkey_of_another_user_cannot_step_up_this_session(self, world):
        await world.repo.create(PHONE_SESSION)
        await world.repo.create(OTHER_SESSION)
        await world.enrol()
        others = await world.enrol(session=OTHER_SESSION, user=OTHER)        # the last credential: the other user's
        with pytest.raises(PasskeyRejected, match="is not a passkey of"):
            await self.step_up(world)
        assert world.repo.sessions["sid-phone"].passkey_at is None
        assert world.repo.passkeys[others.cred_hash] == others                # its counter untouched

    async def test_a_step_up_challenge_serves_one_finish(self, world):
        await world.repo.create(PHONE_SESSION)
        await world.enrol()
        start = await world.service.begin_step_up("user-alex", "sid-phone")
        response = world.authenticator.sign_in(start.options)
        await world.service.finish_step_up(start.nonce, response, "user-alex", "sid-phone")
        with pytest.raises(PasskeyRejected, match="unknown, expired or already used"):
            await world.service.finish_step_up(start.nonce, response, "user-alex", "sid-phone")

    async def test_a_sign_in_challenge_cannot_finish_a_step_up(self, world):
        await world.repo.create(PHONE_SESSION)
        await world.enrol()
        start = await world.service.begin_sign_in()
        with pytest.raises(PasskeyRejected, match="of another ceremony"):
            await world.service.finish_step_up(start.nonce, world.authenticator.sign_in(start.options),
                                               "user-alex", "sid-phone")

    async def test_a_revoked_passkey_cannot_step_up(self, world):
        await world.repo.create(PHONE_SESSION)
        stored = await world.enrol()
        await world.enrol(session=MAC_SESSION)                               # a second, live one keeps the gate on
        await world.service.remove("user-alex", stored.cred_hash)
        with pytest.raises(PasskeyRejected, match="is not a live passkey"):
            await self.step_up(world, cred=world.authenticator.credentials[0])
        assert world.repo.sessions["sid-phone"].passkey_at is None
        assert await world.service.call_start_allowed("user-alex", "sid-phone") is False

    async def test_an_ended_session_records_no_assertion(self, world):
        await world.repo.create(PHONE_SESSION)
        await world.enrol()
        await world.repo.revoke("sid-phone", SessionRevokeReason.LOGOUT, T0)
        with pytest.raises(PasskeyRejected, match="could not record the assertion"):
            await self.step_up(world)
        assert world.repo.sessions["sid-phone"].passkey_at is None

    async def test_a_passkey_sign_in_session_starts_proved(self, world):
        sessions = DeviceSessionService(world.repo, SessionService(secret_key="k" * 40), world.notifier,
                                        clock=world.clock, new_sid=lambda: "sid-new")
        await world.enrol()
        account = BillingAccount(account_id="acct-alex", tier=AccountTier.FAMILY, iam_policy={"user-alex": "owner"})
        await sessions.start(ALEX, account, label=PHONE, platform_hint=PHONE, auth_kind=SessionAuthKind.PASSKEY)
        assert world.repo.sessions["sid-new"].passkey_at == T0
        assert await world.service.call_start_allowed("user-alex", "sid-new") is True


async def test_by_default_the_nonce_and_challenge_are_random_and_the_clock_is_now(world):
    service = PasskeyService(world.repo, world.verifier, world.challenges, world.notifier)
    first, second = await service.begin_registration(ALEX, PHONE_SESSION), await service.begin_sign_in()
    assert len(first.nonce) == 43 and first.nonce != second.nonce                 # 32 random bytes, URL-safe
    assert len(first.options["challenge"]) == 43 and first.options["challenge"] != second.options["challenge"]
    before = datetime.now(timezone.utc)
    stored = await service.finish_registration(first.nonce, world.authenticator.register(first.options), ALEX,
                                               PHONE_SESSION)
    assert before <= stored.created_at <= datetime.now(timezone.utc)
