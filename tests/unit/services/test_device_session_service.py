"""DeviceSessionService (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.5, §7.6) over the in-memory repository twin, the real
`SessionService` and the real `UserNotificationService` posting to recording channels, with a controlled clock.

Covers: `start` (the stored record, the token's sid, the literal new-sign-in notice, a failed notice never failing the
sign-in), every refresh outcome as the caller sees it, `mint_access`, the gate's cache (a hit, the expiry after
`GATE_CACHE_TTL`, a revocation clearing it) and the version counter: a read or a refresh that began before a revocation
never writes its live record back (each interleaving forced with a gate and asserted to have happened)."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.billing import AccountTier, BillingAccount
from src.domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy as P, RefreshOutcome, refresh_secret_hash
from src.domain.user import UserProfile
from src.services.device_session_service import DeviceSessionService
from src.services.localization_service import LocalizationService
from src.services.session_service import SessionService
from src.services.user_notification_service import UserNotificationService
from tests.support.device_session_repo import FakeDeviceSessionRepository
from tests.support.fact_review_world import FakeNotificationState, RecordingChannelFactory

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
SECRET = "test-secret-key-must-be-32-characters-minimum"
ALEX = UserProfile(user_id="user-alex", external_user_id="firebase|alex", email="alex@example.com",
                   display_name="Alex", account_id="acct-alex")
ALEX_ACCOUNT = BillingAccount(account_id="acct-alex", tier=AccountTier.FAMILY, iam_policy={"user-alex": "owner"})
OTHER = UserProfile(user_id="user-other", external_user_id="firebase|other", email="other@example.com",
                    display_name="Other", account_id="acct-other")
PHONE, MAC = "iPhone — Home Screen (call)", "Mac — Chrome"
KILL, LOGOUT = SessionRevokeReason.KILL_SWITCH, SessionRevokeReason.LOGOUT


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def counter(prefix: str):
    n = iter(range(1, 1000))
    return lambda: f"{prefix}-{next(n)}"


class GatedRepo(FakeDeviceSessionRepository):
    """The twin, whose next `get` or `refresh` (once `hold(...)` names it) stops after reading until `release`."""

    def __init__(self) -> None:
        super().__init__()
        self.held: Optional[str] = None
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def hold(self, method: str) -> None:
        self.held = method

    async def _pause(self, method: str) -> None:
        if self.held == method:
            self.held = None
            self.reached.set()
            await self.release.wait()

    async def get(self, sid: str) -> Optional[DeviceSession]:
        found = await super().get(sid)
        await self._pause("get")
        return found

    async def refresh(self, sid, presented_hash, new_hash, now):
        result = await super().refresh(sid, presented_hash, new_hash, now)
        await self._pause("refresh")
        return result


class World:
    def __init__(self, notifications=None) -> None:
        self.clock = Clock()
        self.repo = GatedRepo()
        self.tokens = SessionService(secret_key=SECRET)
        self.state = FakeNotificationState(T0)
        self.channels = RecordingChannelFactory()
        self.notifier = notifications or UserNotificationService(
            self.state, self.channels, coordinator=None, notification_sla={},
            localization=LocalizationService(FileLocalizationAdapter()))
        self.service = DeviceSessionService(self.repo, self.tokens, self.notifier, clock=self.clock,
                                            new_sid=counter("sid"), new_secret=counter("secret"))

    async def start(self, hint: str = PHONE, user: UserProfile = ALEX):
        return await self.service.start(user, ALEX_ACCOUNT, label=hint, platform_hint=hint,
                                        auth_kind=SessionAuthKind.GOOGLE)

    def sid_of(self, token: str) -> str:
        return self.tokens.verify_device_access_token(token, allow_expired=True)["sid"]


@pytest.fixture
async def world():
    w = World()
    await w.state.save("user-alex", "telegram", "chat-alex")
    return w


# ---------------------------------------------------------------------------------------------- start

class TestStart:
    async def test_start_stores_the_session_and_returns_its_token_and_secret(self, world):
        session, token, secret = await world.start()
        assert world.repo.sessions == {"sid-1": session}
        assert session == DeviceSession(
            sid="sid-1", user_id="user-alex", account_id="acct-alex", label=PHONE, platform_hint=PHONE,
            auth_kind=SessionAuthKind.GOOGLE, auth_at=T0, created_at=T0, last_seen_at=T0,
            expires_at=T0 + timedelta(days=30), refresh_hash=refresh_secret_hash("secret-1"),
            purge_after=T0 + timedelta(days=30))
        assert secret == "secret-1"
        claims = world.tokens.verify_device_access_token(token, allow_expired=True)
        assert (claims["sid"], claims["sub"], claims["account_id"], claims["role"], claims["tier"]) == (
            "sid-1", "user-alex", "acct-alex", "owner", "family")
        assert claims["exp"] - claims["iat"] == 15 * 60

    async def test_start_sends_the_new_sign_in_notice_with_the_hint_literally(self, world):
        hostile = "Mac — Chrome <!channel> {time} *bold*"
        await world.start(hint=hostile)
        [(platform, channel, text)] = world.channels.sent_literal
        assert (platform, channel) == ("telegram", "chat-alex")
        assert hostile in text and "09:00 UTC" in text and "$devices off" in text
        assert world.channels.sent == world.channels.sent_literal       # nothing went through the formatter

    async def test_an_undelivered_notice_never_fails_the_sign_in(self, world, caplog):
        world.state.last.clear()                                            # no channel to post to
        session, _, _ = await world.start()
        assert world.repo.sessions == {"sid-1": session}
        assert world.channels.sent == []
        assert "New sign-in notice not delivered" in caplog.text

    async def test_a_raising_notice_never_fails_the_sign_in(self, caplog):
        class Broken:
            async def notify_ui_notice(self, *args, **fields):
                raise RuntimeError("channel down")

        world = World(notifications=Broken())
        session, _, _ = await world.start()
        assert world.repo.sessions == {"sid-1": session}
        assert "New sign-in notice failed" in caplog.text

    async def test_by_default_the_sid_and_secret_are_random_and_the_clock_is_now(self, world):
        service = DeviceSessionService(world.repo, world.tokens, world.notifier)
        before = datetime.now(timezone.utc)
        first, _, first_secret = await service.start(ALEX, ALEX_ACCOUNT, label=MAC, platform_hint=MAC,
                                                     auth_kind=SessionAuthKind.GOOGLE)
        second, _, second_secret = await service.start(ALEX, ALEX_ACCOUNT, label=MAC, platform_hint=MAC,
                                                       auth_kind=SessionAuthKind.GOOGLE)
        assert len(first_secret) == 43 and first_secret != second_secret      # 32 random bytes, URL-safe
        assert len(first.sid) == 22 and first.sid != second.sid               # 16 random bytes
        assert before <= first.created_at <= datetime.now(timezone.utc)

    async def test_a_taken_sid_refuses_the_start(self, world):
        world.service._new_sid = lambda: "sid-1"
        await world.start()
        with pytest.raises(RuntimeError, match="taken"):
            await world.start(hint=MAC)
        assert world.repo.sessions["sid-1"].label == PHONE
        assert len(world.channels.sent) == 1


# ---------------------------------------------------------------------------------------------- refresh

class TestPasskeyStart:
    """A passkey sign-in (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5): it sends no new-sign-in notice, and the session
    starts proved by a passkey for a call start; a Google one is announced and does not."""

    async def test_a_passkey_start_sends_no_notice_and_still_creates_the_session(self, world):
        session, token, secret = await world.service.start(
            ALEX, ALEX_ACCOUNT, label=PHONE, platform_hint=PHONE, auth_kind=SessionAuthKind.PASSKEY)
        assert world.channels.sent == [] and world.channels.sent_literal == []
        assert world.repo.sessions == {"sid-1": session}
        assert (session.auth_kind, session.passkey_at) == (SessionAuthKind.PASSKEY, T0)
        assert world.sid_of(token) == "sid-1" and secret == "secret-1"

    async def test_a_google_session_is_announced_as_google_and_starts_unproved(self, world):
        session, _, _ = await world.start()
        [(_, _, text)] = world.channels.sent_literal
        assert "Google" in text and "passkey" not in text
        assert session.passkey_at is None


class TestCurrent:
    async def test_it_is_the_live_record_of_the_users_own_session(self, world):
        session, _, _ = await world.start()
        assert await world.service.current("user-alex", session.sid) == session

    async def test_another_users_unknown_or_ended_session_is_none(self, world):
        session, _, _ = await world.start()
        assert await world.service.current("user-other", session.sid) is None
        assert await world.service.current("user-alex", "sid-unknown") is None
        world.clock.advance(days=14)
        assert await world.service.current("user-alex", session.sid) is None


class TestRefresh:
    async def test_rotate_returns_a_new_token_and_secret_and_the_old_secret_then_gets_grace(self, world):
        await world.start()
        world.clock.advance(minutes=20)
        result = await world.service.refresh("sid-1", "secret-1", ALEX, ALEX_ACCOUNT)
        assert (result.outcome, result.refresh_secret) == (RefreshOutcome.ROTATE, "secret-2")
        assert world.sid_of(result.access_token) == "sid-1"
        assert world.repo.sessions["sid-1"].refresh_hash == refresh_secret_hash("secret-2")
        assert result.session == world.repo.sessions["sid-1"]

        world.clock.advance(seconds=59)
        grace = await world.service.refresh("sid-1", "secret-1", ALEX, ALEX_ACCOUNT)
        assert (grace.outcome, grace.refresh_secret) == (RefreshOutcome.GRACE, None)
        assert world.sid_of(grace.access_token) == "sid-1"
        assert world.repo.sessions["sid-1"].refresh_hash == refresh_secret_hash("secret-2")

        rotated = await world.service.refresh("sid-1", "secret-2", ALEX, ALEX_ACCOUNT)
        assert (rotated.outcome, rotated.refresh_secret) == (RefreshOutcome.ROTATE, "secret-4")

    async def test_an_old_secret_after_the_grace_revokes_the_session_at_once(self, world):
        await world.start()
        await world.service.refresh("sid-1", "secret-1", ALEX, ALEX_ACCOUNT)
        assert await world.service.is_live("sid-1") is True                # cached live
        world.clock.advance(seconds=60)
        result = await world.service.refresh("sid-1", "secret-1", ALEX, ALEX_ACCOUNT)
        assert (result.outcome, result.access_token, result.refresh_secret) == (RefreshOutcome.REPLAY, None, None)
        assert world.repo.sessions["sid-1"].revoke_reason is SessionRevokeReason.REPLAY
        assert await world.service.is_live("sid-1") is False               # this instance knows at once
        dead = await world.service.refresh("sid-1", "secret-2", ALEX, ALEX_ACCOUNT)
        assert (dead.outcome, dead.access_token, dead.refresh_secret) == (RefreshOutcome.DEAD, None, None)

    async def test_an_unknown_sid_is_dead(self, world):
        result = await world.service.refresh("sid-unknown", "secret-1", ALEX, ALEX_ACCOUNT)
        assert (result.outcome, result.session, result.access_token) == (RefreshOutcome.DEAD, None, None)

    async def test_a_session_of_another_user_mints_nothing(self, world, caplog):
        await world.start()
        result = await world.service.refresh("sid-1", "secret-1", OTHER, ALEX_ACCOUNT)
        assert (result.outcome, result.access_token, result.refresh_secret) == (RefreshOutcome.DEAD, None, None)
        assert "belongs to another user" in caplog.text

    async def test_mint_access_carries_the_sid_and_refuses_another_user(self, world):
        session, _, _ = await world.start()
        world.clock.advance(hours=1)
        claims = world.tokens.verify_device_access_token(world.service.mint_access(session, ALEX, ALEX_ACCOUNT),
                                                         allow_expired=True)
        assert (claims["sid"], claims["iat"]) == ("sid-1", int((T0 + timedelta(hours=1)).timestamp()))
        with pytest.raises(ValueError):
            world.service.mint_access(session, OTHER, ALEX_ACCOUNT)


# ---------------------------------------------------------------------------------------------- the gate

class TestGate:
    async def test_the_gate_answers_from_its_cache_until_the_ttl_ends(self, world):
        await world.start()
        world.clock.advance(seconds=1)
        world.service._cache.clear()                                        # a gate check on another instance
        assert await world.service.is_live("sid-1") is True
        await world.repo.revoke("sid-1", KILL, world.clock())               # revoked elsewhere
        world.clock.advance(seconds=29)
        assert await world.service.is_live("sid-1") is True                 # within GATE_CACHE_TTL: the cache
        world.clock.advance(seconds=1)
        assert await world.service.is_live("sid-1") is False                # at its end: the record

    async def test_an_unknown_sid_is_not_live_and_stays_cached(self, world):
        assert await world.service.is_live("sid-unknown") is False
        assert world.service._cache["sid-unknown"] == (None, T0)
        assert await world.service.is_live("sid-unknown") is False

    async def test_a_cached_session_past_its_idle_end_is_not_live(self, world):
        await world.start()
        world.clock.now = T0 + P.IDLE_TTL - timedelta(seconds=1)
        world.service._cache.clear()
        assert await world.service.is_live("sid-1") is True
        world.clock.advance(seconds=1)                                      # the idle end, the cache still fresh
        assert await world.service.is_live("sid-1") is False

    async def test_revoke_clears_this_instances_cache(self, world):
        await world.start()
        assert await world.service.is_live("sid-1") is True
        assert await world.service.revoke("user-alex", "sid-1", LOGOUT) is True
        assert await world.service.is_live("sid-1") is False
        assert await world.service.revoke("user-alex", "sid-1", LOGOUT) is False     # already revoked

    async def test_a_read_begun_before_a_revoke_does_not_write_its_live_record_back(self, world):
        await world.start()
        world.service._cache.clear()
        world.repo.hold("get")
        reading = asyncio.create_task(world.service.is_live("sid-1"))
        await asyncio.wait_for(world.repo.reached.wait(), 1)               # the read holds the live record
        assert await world.service.revoke("user-alex", "sid-1", LOGOUT) is True
        world.repo.release.set()
        assert await reading is True                                        # it began before the revoke
        assert "sid-1" not in world.service._cache
        assert await world.service.is_live("sid-1") is False

    async def test_a_read_begun_before_revoke_all_does_not_write_its_live_record_back(self, world):
        await world.start()
        world.service._cache.clear()
        world.repo.hold("get")
        reading = asyncio.create_task(world.service.is_live("sid-1"))
        await asyncio.wait_for(world.repo.reached.wait(), 1)
        assert await world.service.revoke_all("user-alex", KILL) == 1
        world.repo.release.set()
        assert await reading is True
        assert "sid-1" not in world.service._cache
        assert await world.service.is_live("sid-1") is False

    async def test_a_refresh_begun_before_a_revoke_does_not_cache_the_rotated_record(self, world):
        await world.start()
        world.repo.hold("refresh")
        refreshing = asyncio.create_task(world.service.refresh("sid-1", "secret-1", ALEX, ALEX_ACCOUNT))
        await asyncio.wait_for(world.repo.reached.wait(), 1)               # rotated, not yet returned
        assert await world.service.revoke("user-alex", "sid-1", LOGOUT) is True
        world.repo.release.set()
        assert (await refreshing).outcome is RefreshOutcome.ROTATE
        assert "sid-1" not in world.service._cache
        assert await world.service.is_live("sid-1") is False


# ---------------------------------------------------------------------------------------------- revocation, the list

class TestRevocation:
    async def test_revoke_refuses_a_sid_of_another_user_and_an_unknown_sid(self, world, caplog):
        await world.start()
        assert await world.service.revoke("user-other", "sid-1", LOGOUT) is False
        assert world.repo.sessions["sid-1"].revoked_at is None
        assert await world.service.revoke("user-alex", "sid-unknown", LOGOUT) is False
        assert "is not a session of" in caplog.text

    async def test_revoke_all_ends_every_session_of_the_user_and_the_list_shows_live_ones_only(self, world):
        await world.start(PHONE)
        world.clock.advance(minutes=1)
        await world.start(MAC)
        await world.start(MAC, user=OTHER)
        assert [s.sid for s in await world.service.list_for_user("user-alex")] == ["sid-1", "sid-2"]
        assert await world.service.is_live("sid-1") is True
        assert await world.service.revoke_all("user-alex", KILL) == 2
        assert await world.service.list_for_user("user-alex") == []
        assert await world.service.is_live("sid-1") is False                # the cache was cleared
        assert await world.service.is_live("sid-3") is True                 # another user's session lives
        assert world.repo.sessions["sid-2"].revoke_reason is KILL


# ---------------------------------------------------------------------------------------------- the connector grants (§7.6)

class Grants:
    """The MCP refresh-token store's user-wide revocation as `revoke_all` sees it: the user's live tokens by name."""

    def __init__(self) -> None:
        self.live = {"user-alex": {"rt-claude", "rt-desktop"}, "user-other": {"rt-other"}}
        self.revoked_at = {}

    async def revoke(self, user_id, now):
        tokens = self.live.pop(user_id, set())
        for token in tokens:
            self.revoked_at[token] = now
        return len(tokens)


class TestConnectorGrants:
    async def test_the_kill_switch_also_ends_every_connector_grant_of_the_user(self, world):
        grants = Grants()
        service = DeviceSessionService(world.repo, world.tokens, world.notifier, clock=world.clock,
                                       new_sid=counter("sid"), new_secret=counter("secret"),
                                       revoke_connector_grants=grants.revoke)
        await service.start(ALEX, ALEX_ACCOUNT, label=PHONE, platform_hint=PHONE, auth_kind=SessionAuthKind.GOOGLE)
        world.clock.advance(minutes=3)
        assert await service.revoke_all("user-alex", KILL) == 1
        assert grants.revoked_at == {"rt-claude": T0 + timedelta(minutes=3), "rt-desktop": T0 + timedelta(minutes=3)}
        assert grants.live == {"user-other": {"rt-other"}}
        assert world.repo.sessions["sid-1"].revoked_at == T0 + timedelta(minutes=3)

    async def test_the_grants_are_revoked_even_when_the_sessions_could_not_be(self, world):
        class Down(GatedRepo):
            async def revoke_all(self, user_id, reason, now):
                raise RuntimeError("store down")

        grants = Grants()
        service = DeviceSessionService(Down(), world.tokens, world.notifier, clock=world.clock,
                                       revoke_connector_grants=grants.revoke)
        with pytest.raises(RuntimeError, match="store down"):
            await service.revoke_all("user-alex", KILL)
        assert set(grants.revoked_at) == {"rt-claude", "rt-desktop"}
