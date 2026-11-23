"""FirestoreDeviceSessionRepository over the kit backend (`kit_db`: `EvalDb` with index refusal in the unit suite, the
test database in `make test-cloud`), and the port's rules run on both it and the in-memory twin
(`tests/support/device_session_repo.py`) so the two cannot drift (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.6): create never
overwrites, each refresh outcome and exactly its write, one ROTATE out of two concurrent refreshes, the first revocation
kept, `revoke_all` over the user's sessions only. Firestore-only cases assert the raw stored documents, that the secret
never reaches them, and that a document contradicting its id is absent."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.firestore_device_session_repo import FirestoreDeviceSessionRepository
from src.domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy as P, RefreshOutcome, refresh_secret_hash
from tests.support.device_session_repo import FakeDeviceSessionRepository

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SESSIONS = "eval_auth_device_sessions_v1"
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
FIRST, SECOND, THIRD = (refresh_secret_hash(s) for s in ("secret-1", "secret-2", "secret-3"))
LOGOUT, LIST, KILL, REPLAY = (SessionRevokeReason.LOGOUT, SessionRevokeReason.DEVICE_LIST,
                              SessionRevokeReason.KILL_SWITCH, SessionRevokeReason.REPLAY)


def session(sid="sid-1", user_id="user-alex", minutes=0, **over) -> DeviceSession:
    created = T0 + timedelta(minutes=minutes)
    base = dict(sid=sid, user_id=user_id, account_id="acct-alex", label="iPhone — Home Screen (call)",
                platform_hint="iPhone — Home Screen (call)", auth_kind=SessionAuthKind.GOOGLE, auth_at=created,
                created_at=created, last_seen_at=created, expires_at=P.absolute_end(created), refresh_hash=FIRST,
                purge_after=P.purge_after(created))
    base.update(over)
    return DeviceSession(**base)


def firestore_repo(kit_db):
    db = kit_db(indexes=INDEXES_FILE, groups={SESSIONS: "development_auth_device_sessions_v1"})
    return FirestoreDeviceSessionRepository(db, SESSIONS), db


@pytest.fixture(params=["firestore", "fake"])
def repo(request, kit_db):
    return firestore_repo(kit_db)[0] if request.param == "firestore" else FakeDeviceSessionRepository()


# ---------------------------------------------------------------------------------------------- the port's rules

class TestPortRules:
    async def test_a_session_round_trips_with_every_field(self, repo):
        stored = session(prev_refresh_hash=SECOND, rotated_at=T0 + timedelta(hours=2, microseconds=7),
                         last_seen_at=T0 + timedelta(hours=3), revoked_at=T0 + timedelta(hours=4),
                         revoke_reason=LIST, label="Mac — Chrome", platform_hint="Mac — Chrome {x}")
        assert await repo.create(stored) is True
        assert await repo.get("sid-1") == stored

    async def test_create_never_overwrites(self, repo):
        assert await repo.create(session()) is True
        assert await repo.create(session(label="Mac — Chrome")) is False
        assert (await repo.get("sid-1")).label == "iPhone — Home Screen (call)"

    async def test_an_unknown_sid_reads_as_none(self, repo):
        assert await repo.get("sid-unknown") is None
        assert await repo.list_for_user("user-alex") == []

    async def test_list_for_user_is_oldest_first_holds_revoked_ones_and_no_other_users(self, repo):
        await repo.create(session("sid-c", minutes=5))
        await repo.create(session("sid-b", minutes=1, revoked_at=T0, revoke_reason=LOGOUT))
        await repo.create(session("sid-a", minutes=1))
        await repo.create(session("sid-z", user_id="user-other"))
        assert [s.sid for s in await repo.list_for_user("user-alex")] == ["sid-a", "sid-b", "sid-c"]
        assert [s.sid for s in await repo.list_for_user("user-other")] == ["sid-z"]

    async def test_rotate_shifts_the_hashes_and_stamps_the_use(self, repo):
        await repo.create(session())
        now = T0 + timedelta(days=3)
        outcome, after = await repo.refresh("sid-1", FIRST, SECOND, now)
        assert outcome is RefreshOutcome.ROTATE
        expected = session(refresh_hash=SECOND, prev_refresh_hash=FIRST, rotated_at=now, last_seen_at=now,
                           purge_after=P.purge_after(now))
        assert after == expected and await repo.get("sid-1") == expected

    async def test_grace_moves_only_last_seen(self, repo):
        await repo.create(session())
        rotated = T0 + timedelta(days=3)
        await repo.refresh("sid-1", FIRST, SECOND, rotated)
        now = rotated + timedelta(seconds=30)
        outcome, after = await repo.refresh("sid-1", FIRST, THIRD, now)
        assert outcome is RefreshOutcome.GRACE
        expected = session(refresh_hash=SECOND, prev_refresh_hash=FIRST, rotated_at=rotated, last_seen_at=now,
                           purge_after=P.purge_after(rotated))
        assert after == expected and await repo.get("sid-1") == expected

    @pytest.mark.parametrize("presented, after_rotation", [(FIRST, timedelta(seconds=60)), (THIRD, timedelta(0))],
                             ids=["previous-after-grace", "neither"])
    async def test_replay_revokes_and_keeps_the_hashes(self, repo, presented, after_rotation):
        await repo.create(session())
        rotated = T0 + timedelta(days=3)
        await repo.refresh("sid-1", FIRST, SECOND, rotated)
        now = rotated + after_rotation
        outcome, after = await repo.refresh("sid-1", presented, "unused", now)
        assert outcome is RefreshOutcome.REPLAY
        expected = session(refresh_hash=SECOND, prev_refresh_hash=FIRST, rotated_at=rotated, last_seen_at=rotated,
                           purge_after=P.purge_after(rotated), revoked_at=now, revoke_reason=REPLAY)
        assert after == expected and await repo.get("sid-1") == expected

    @pytest.mark.parametrize("stored, now", [
        (session(revoked_at=T0, revoke_reason=LOGOUT), T0 + timedelta(hours=1)),
        (session(), T0 + P.IDLE_TTL),
        (session(last_seen_at=T0 + timedelta(days=29)), T0 + P.ABSOLUTE_TTL),
    ], ids=["revoked", "idle", "absolute"])
    async def test_dead_writes_nothing(self, repo, stored, now):
        await repo.create(stored)
        assert await repo.refresh("sid-1", FIRST, SECOND, now) == (RefreshOutcome.DEAD, stored)
        assert await repo.get("sid-1") == stored

    async def test_an_unknown_sid_refreshes_dead_and_creates_nothing(self, repo):
        assert await repo.refresh("sid-unknown", FIRST, SECOND, T0) == (RefreshOutcome.DEAD, None)
        assert await repo.get("sid-unknown") is None

    async def test_of_two_concurrent_refreshes_with_one_secret_exactly_one_rotates(self, repo):
        await repo.create(session())
        if isinstance(repo, FirestoreDeviceSessionRepository):
            repo._col.yield_on_get = True      # both transactions read the record before either commits
        now = T0 + timedelta(days=1)
        results = await asyncio.gather(repo.refresh("sid-1", FIRST, SECOND, now),
                                       repo.refresh("sid-1", FIRST, THIRD, now))
        outcomes = sorted(outcome.value for outcome, _ in results)
        assert outcomes == [RefreshOutcome.GRACE.value, RefreshOutcome.ROTATE.value]
        winner = SECOND if results[0][0] is RefreshOutcome.ROTATE else THIRD
        stored = await repo.get("sid-1")
        assert (stored.refresh_hash, stored.prev_refresh_hash, stored.revoked_at) == (winner, FIRST, None)

    async def test_revoke_keeps_the_first_revocation(self, repo):
        await repo.create(session())
        first, later = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
        assert await repo.revoke("sid-1", LOGOUT, first) is True
        assert await repo.revoke("sid-1", LIST, later) is False
        stored = await repo.get("sid-1")
        assert (stored.revoked_at, stored.revoke_reason) == (first, LOGOUT)
        assert stored == session(revoked_at=first, revoke_reason=LOGOUT)
        assert await repo.revoke("sid-unknown", LOGOUT, first) is False
        assert await repo.get("sid-unknown") is None

    async def test_revoke_all_revokes_the_users_unrevoked_sessions_only(self, repo):
        await repo.create(session("sid-a"))
        await repo.create(session("sid-b", minutes=1))
        await repo.create(session("sid-c", minutes=2, revoked_at=T0, revoke_reason=LOGOUT))
        await repo.create(session("sid-z", user_id="user-other"))
        now = T0 + timedelta(hours=1)
        assert await repo.revoke_all("user-alex", KILL, now) == 2
        assert [(s.sid, s.revoked_at, s.revoke_reason) for s in await repo.list_for_user("user-alex")] == [
            ("sid-a", now, KILL), ("sid-b", now, KILL), ("sid-c", T0, LOGOUT)]
        assert (await repo.get("sid-z")).revoked_at is None
        assert await repo.revoke_all("user-alex", KILL, now) == 0
        assert await repo.revoke_all("user-nobody", KILL, now) == 0


# ---------------------------------------------------------------------------------------------- Firestore only

class TestFirestoreDocuments:
    async def test_the_stored_document_holds_values_and_never_the_secret(self, kit_db):
        repo, db = firestore_repo(kit_db)
        await repo.create(session())
        await repo.refresh("sid-1", FIRST, SECOND, T0 + timedelta(days=1))
        await repo.revoke("sid-1", LIST, T0 + timedelta(days=2))
        doc = db.collection(SESSIONS).docs["sid-1"]
        assert set(doc) == {"sid", "user_id", "account_id", "label", "platform_hint", "auth_kind", "auth_at",
                            "created_at", "last_seen_at", "expires_at", "refresh_hash", "prev_refresh_hash",
                            "rotated_at", "revoked_at", "revoke_reason", "purge_after"}
        assert (doc["auth_kind"], doc["revoke_reason"]) == ("google", "device_list")
        assert (doc["refresh_hash"], doc["prev_refresh_hash"]) == (SECOND, FIRST)
        assert doc["purge_after"] == T0 + timedelta(days=1) + P.PURGE_AFTER
        assert "secret-1" not in str(doc) and "secret-2" not in str(doc)

    async def test_a_document_contradicting_its_id_is_absent_everywhere(self, kit_db, caplog):
        repo, db = firestore_repo(kit_db)
        await repo.create(session("sid-real"))
        forged = {k: v for k, v in db.collection(SESSIONS).docs["sid-real"].items()}
        forged["sid"] = "sid-real"
        db.collection(SESSIONS).docs["sid-forged"] = forged
        assert await repo.get("sid-forged") is None
        assert [s.sid for s in await repo.list_for_user("user-alex")] == ["sid-real"]
        assert await repo.refresh("sid-forged", FIRST, SECOND, T0) == (RefreshOutcome.DEAD, None)
        assert await repo.revoke("sid-forged", LOGOUT, T0) is False
        assert await repo.revoke_all("user-alex", KILL, T0) == 1
        assert db.collection(SESSIONS).docs["sid-forged"].get("revoked_at") is None
        assert "does not match its stored sid" in caplog.text
