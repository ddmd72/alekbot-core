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
from src.adapters.firestore_facts_pin_repo import FirestoreFactsPinRepository
from src.domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy as P, RefreshOutcome, refresh_secret_hash
from src.domain.passkey import Passkey, PasskeyKind
from tests.support.device_session_repo import FakeDeviceSessionRepository
from tests.support.facts_pin_repo import FakeFactsPinRepository

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SESSIONS = "eval_auth_device_sessions_v1"
PASSKEYS = "eval_auth_passkeys_v1"
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


def passkey(cred_hash="key-1", user_id="user-alex", minutes=0, **over) -> Passkey:
    created = T0 + timedelta(minutes=minutes)
    base = dict(cred_hash=cred_hash, credential_id=b"\x00cred-" + cred_hash.encode(), user_id=user_id,
                account_id="acct-alex", kind=PasskeyKind.WEBAUTHN, public_key=b"\xa5\x01\x02cose", algorithm=-7,
                sign_count=0, label="iPhone — Home Screen (call)", platform_hint="iPhone — Home Screen (call)",
                backup_eligible=True, created_at=created)
    base.update(over)
    return Passkey(**base)


def firestore_repo(kit_db):
    db = kit_db(indexes=INDEXES_FILE, groups={SESSIONS: "development_auth_device_sessions_v1",
                                              PASSKEYS: "development_auth_passkeys_v1"})
    return FirestoreDeviceSessionRepository(db, SESSIONS, PASSKEYS), db


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

    async def test_revoke_all_also_revokes_the_users_live_passkeys_and_counts_sessions(self, repo):
        await repo.create(session("sid-a"))
        await repo.add_passkey(passkey("key-a"))
        await repo.add_passkey(passkey("key-b", minutes=1, revoked_at=T0))
        await repo.add_passkey(passkey("key-z", user_id="user-other"))
        now = T0 + timedelta(hours=1)
        assert await repo.revoke_all("user-alex", KILL, now) == 1
        assert [(k.cred_hash, k.revoked_at) for k in await repo.list_passkeys("user-alex")] == [
            ("key-a", now), ("key-b", T0)]
        assert (await repo.get_passkey("key-z")).revoked_at is None
        assert await repo.revoke_all("user-nobody", KILL, now) == 0


class TestPasskeyRules:
    async def test_a_passkey_round_trips_with_every_field(self, repo):
        stored = passkey(sign_count=7, backup_eligible=False, label="Mac — Chrome", platform_hint="Mac — Chrome {x}",
                         last_used_at=T0 + timedelta(hours=2, microseconds=7), revoked_at=T0 + timedelta(hours=3))
        assert await repo.add_passkey(stored) is True
        assert await repo.get_passkey("key-1") == stored

    async def test_add_never_overwrites(self, repo):
        assert await repo.add_passkey(passkey()) is True
        assert await repo.add_passkey(passkey(label="Mac — Chrome")) is False
        assert (await repo.get_passkey("key-1")).label == "iPhone — Home Screen (call)"

    async def test_an_unknown_cred_hash_reads_as_none(self, repo):
        assert await repo.get_passkey("key-unknown") is None
        assert await repo.list_passkeys("user-alex") == []

    async def test_list_is_oldest_first_holds_revoked_ones_and_no_other_users(self, repo):
        await repo.add_passkey(passkey("key-c", minutes=5))
        await repo.add_passkey(passkey("key-b", minutes=1, revoked_at=T0))
        await repo.add_passkey(passkey("key-a", minutes=1))
        await repo.add_passkey(passkey("key-z", user_id="user-other"))
        assert [k.cred_hash for k in await repo.list_passkeys("user-alex")] == ["key-a", "key-b", "key-c"]
        assert [k.cred_hash for k in await repo.list_passkeys("user-other")] == ["key-z"]

    @pytest.mark.parametrize("stored_count, presented, touched", [
        (0, 0, True), (0, 1, True), (4, 5, True), (5, 5, False), (5, 4, False), (5, 0, False),
    ], ids=["zero-zero", "first-count", "advance", "same", "lower", "zero-after-nonzero"])
    async def test_touch_writes_only_an_advancing_counter(self, repo, stored_count, presented, touched):
        await repo.add_passkey(passkey(sign_count=stored_count))
        used = T0 + timedelta(days=1)
        assert await repo.touch_passkey("key-1", presented, used) is touched
        expected = passkey(sign_count=presented, last_used_at=used) if touched else passkey(sign_count=stored_count)
        assert await repo.get_passkey("key-1") == expected

    async def test_touch_refuses_an_absent_or_revoked_passkey(self, repo):
        await repo.add_passkey(passkey(revoked_at=T0))
        assert await repo.touch_passkey("key-1", 1, T0 + timedelta(hours=1)) is False
        assert await repo.get_passkey("key-1") == passkey(revoked_at=T0)
        assert await repo.touch_passkey("key-unknown", 1, T0) is False
        assert await repo.get_passkey("key-unknown") is None

    async def test_of_two_concurrent_touches_with_one_counter_exactly_one_lands(self, repo):
        await repo.add_passkey(passkey(sign_count=3))
        if isinstance(repo, FirestoreDeviceSessionRepository):
            repo._passkeys.yield_on_get = True     # both transactions read the record before either commits
        first, second = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
        results = await asyncio.gather(repo.touch_passkey("key-1", 4, first), repo.touch_passkey("key-1", 4, second))
        assert sorted(results) == [False, True]
        stored = await repo.get_passkey("key-1")
        assert (stored.sign_count, stored.last_used_at) == (4, first if results[0] else second)

    async def test_revoke_keeps_the_first_revocation(self, repo):
        await repo.add_passkey(passkey())
        first, later = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
        assert await repo.revoke_passkey("key-1", first) is True
        assert await repo.revoke_passkey("key-1", later) is False
        assert await repo.get_passkey("key-1") == passkey(revoked_at=first)
        assert await repo.revoke_passkey("key-unknown", first) is False
        assert await repo.get_passkey("key-unknown") is None


# ---------------------------------------------------------------------------------------------- Firestore only

class TestFirestoreDocuments:
    async def test_the_stored_document_holds_values_and_never_the_secret(self, kit_db):
        repo, db = firestore_repo(kit_db)
        await repo.create(session())
        await repo.refresh("sid-1", FIRST, SECOND, T0 + timedelta(days=1))
        await repo.revoke("sid-1", LIST, T0 + timedelta(days=2))
        doc = db.collection(SESSIONS).docs["sid-1"]
        assert set(doc) == {"sid", "user_id", "account_id", "label", "platform_hint", "auth_kind", "auth_at",
                            "passkey_at", "created_at", "last_seen_at", "expires_at", "refresh_hash", "prev_refresh_hash",
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

    async def test_the_stored_passkey_document_holds_values_and_bytes(self, kit_db):
        repo, db = firestore_repo(kit_db)
        await repo.add_passkey(passkey())
        await repo.touch_passkey("key-1", 3, T0 + timedelta(days=1))
        await repo.revoke_passkey("key-1", T0 + timedelta(days=2))
        doc = db.collection(PASSKEYS).docs["key-1"]
        assert set(doc) == {"cred_hash", "credential_id", "user_id", "account_id", "kind", "public_key", "algorithm",
                            "sign_count", "label", "platform_hint", "backup_eligible", "created_at", "last_used_at",
                            "revoked_at"}
        assert (doc["kind"], doc["credential_id"], doc["public_key"]) == ("webauthn", b"\x00cred-key-1",
                                                                          b"\xa5\x01\x02cose")
        assert (doc["sign_count"], doc["last_used_at"], doc["revoked_at"]) == (
            3, T0 + timedelta(days=1), T0 + timedelta(days=2))
        assert db.collection(SESSIONS).docs == {}

    async def test_a_passkey_document_contradicting_its_id_is_absent_everywhere(self, kit_db, caplog):
        repo, db = firestore_repo(kit_db)
        await repo.add_passkey(passkey("key-real"))
        forged = {k: v for k, v in db.collection(PASSKEYS).docs["key-real"].items()}
        db.collection(PASSKEYS).docs["key-forged"] = forged
        assert await repo.get_passkey("key-forged") is None
        assert [k.cred_hash for k in await repo.list_passkeys("user-alex")] == ["key-real"]
        assert await repo.touch_passkey("key-forged", 1, T0) is False
        assert await repo.revoke_passkey("key-forged", T0) is False
        await repo.revoke_all("user-alex", KILL, T0)
        assert db.collection(PASSKEYS).docs["key-forged"].get("revoked_at") is None
        assert db.collection(PASSKEYS).docs["key-real"]["revoked_at"] == T0
        assert "does not match its stored cred_hash" in caplog.text


# ---------------------------------------------------------------------------------------------- passkey assertions (§7.3)

class TestPasskeyAssertion:
    """`record_passkey_assertion`: the call-start step-up written on the session."""

    async def test_it_sets_passkey_at_and_nothing_else(self, repo):
        stored = session()
        await repo.create(stored)
        at = T0 + timedelta(hours=1, microseconds=5)
        assert await repo.record_passkey_assertion("sid-1", "user-alex", at) is True
        assert await repo.get("sid-1") == stored.model_copy(update={"passkey_at": at})

    async def test_a_later_assertion_replaces_the_earlier_one(self, repo):
        await repo.create(session())
        await repo.record_passkey_assertion("sid-1", "user-alex", T0 + timedelta(minutes=1))
        assert await repo.record_passkey_assertion("sid-1", "user-alex", T0 + timedelta(minutes=9)) is True
        assert (await repo.get("sid-1")).passkey_at == T0 + timedelta(minutes=9)

    async def test_an_unknown_sid_is_refused_and_creates_nothing(self, repo):
        assert await repo.record_passkey_assertion("sid-unknown", "user-alex", T0) is False
        assert await repo.get("sid-unknown") is None

    async def test_another_users_session_is_refused_and_untouched(self, repo):
        await repo.create(session())
        assert await repo.record_passkey_assertion("sid-1", "user-other", T0) is False
        assert (await repo.get("sid-1")).passkey_at is None

    async def test_a_revoked_session_is_refused(self, repo):
        await repo.create(session())
        await repo.revoke("sid-1", LOGOUT, T0)
        assert await repo.record_passkey_assertion("sid-1", "user-alex", T0 + timedelta(seconds=1)) is False
        assert (await repo.get("sid-1")).passkey_at is None

    @pytest.mark.parametrize("after", [P.IDLE_TTL, P.ABSOLUTE_TTL])
    async def test_a_session_past_its_end_is_refused(self, repo, after):
        await repo.create(session())
        assert await repo.record_passkey_assertion("sid-1", "user-alex", T0 + after) is False
        assert (await repo.get("sid-1")).passkey_at is None

    async def test_firestore_stores_the_field_and_an_old_document_reads_it_as_none(self, kit_db):
        repo, db = firestore_repo(kit_db)
        await repo.create(session())
        await repo.record_passkey_assertion("sid-1", "user-alex", T0 + timedelta(minutes=3))
        assert db.collection(SESSIONS).docs["sid-1"]["passkey_at"] == T0 + timedelta(minutes=3)
        old = {k: v for k, v in db.collection(SESSIONS).docs["sid-1"].items() if k != "passkey_at"}
        old["sid"] = "sid-old"
        db.collection(SESSIONS).docs["sid-old"] = old                     # a record written before the field existed
        assert (await repo.get("sid-old")).passkey_at is None

    async def test_firestore_logs_a_refusal_without_the_sid_in_full(self, kit_db, caplog):
        repo, _ = firestore_repo(kit_db)
        await repo.record_passkey_assertion("sid-unknown-0123456789", "user-alex", T0)
        assert "Passkey assertion not recorded" in caplog.text and "sid-unknown-0123456789" not in caplog.text


# ---------------------------------------------------------------------------------------------- the facts PIN (§7.6, §7.7)

PINS, USERS = "eval_facts_pin_v1", "eval_domain_users_v2"


class LinkedStores:
    """The device store and the facts PIN store on one backend, linked as production links them."""

    def __init__(self, kind, kit_db) -> None:
        if kind == "firestore":
            db = kit_db(indexes=INDEXES_FILE, groups={SESSIONS: "development_auth_device_sessions_v1",
                                                      PASSKEYS: "development_auth_passkeys_v1",
                                                      PINS: "development_facts_pin_v1",
                                                      USERS: "development_domain_users_v2"})
            self.pins = FirestoreFactsPinRepository(db, PINS, USERS)
            self.repo = FirestoreDeviceSessionRepository(db, SESSIONS, PASSKEYS, facts_pin_collection=PINS)
        else:
            self.pins = FakeFactsPinRepository()
            self.repo = FakeDeviceSessionRepository(facts_pins=self.pins)


@pytest.fixture(params=["firestore", "fake"])
def linked(request, kit_db):
    return LinkedStores(request.param, kit_db)


class TestFactsPinRules:
    async def test_a_session_round_trips_with_its_facts_unlock(self, repo):
        stored = session(facts_unlocked_until=T0 + timedelta(minutes=15, microseconds=3))
        assert await repo.create(stored) is True
        assert await repo.get("sid-1") == stored

    async def test_unlock_and_relock_write_only_the_unlock(self, repo):
        await repo.create(session())
        until = T0 + timedelta(minutes=20)
        assert await repo.set_facts_unlock("sid-1", until, T0 + timedelta(minutes=5)) is True
        assert await repo.get("sid-1") == session(facts_unlocked_until=until)
        assert await repo.set_facts_unlock("sid-1", None, T0 + timedelta(minutes=6)) is True
        assert await repo.get("sid-1") == session()

    @pytest.mark.parametrize("stored, now", [
        (session(revoked_at=T0, revoke_reason=LOGOUT), T0 + timedelta(minutes=1)),
        (session(), T0 + P.IDLE_TTL),
    ], ids=["revoked", "idle"])
    async def test_a_dead_session_is_never_unlocked(self, repo, stored, now):
        await repo.create(stored)
        assert await repo.set_facts_unlock("sid-1", now + timedelta(minutes=15), now) is False
        assert await repo.get("sid-1") == stored
        assert await repo.set_facts_unlock("sid-unknown", now, now) is False
        assert await repo.get("sid-unknown") is None

    async def test_revoke_all_cancels_the_users_pending_reset_and_keeps_the_codes(self, linked):
        await linked.pins.set_codes("user-alex", "pin-digest", "duress-digest", T0, replace=False)
        await linked.pins.request_reset("user-alex", T0)
        await linked.pins.set_codes("user-other", "pin-other", "duress-other", T0, replace=False)
        await linked.pins.request_reset("user-other", T0)
        await linked.repo.create(session("sid-a"))
        now = T0 + timedelta(hours=1)
        assert await linked.repo.revoke_all("user-alex", KILL, now) == 1
        alex = await linked.pins.get("user-alex")
        assert (alex.reset_requested_at, alex.reset_due_at, alex.updated_at) == (None, None, now)
        assert (alex.pin_digest, alex.duress_digest) == ("pin-digest", "duress-digest")
        assert (await linked.pins.get("user-other")).reset_requested_at == T0
        assert (await linked.repo.get("sid-a")).revoked_at == now

    async def test_revoke_all_without_a_pending_reset_or_a_pin_touches_no_pin_record(self, linked):
        await linked.pins.set_codes("user-alex", "pin-digest", "duress-digest", T0, replace=False)
        before = await linked.pins.get("user-alex")
        assert await linked.repo.revoke_all("user-alex", KILL, T0 + timedelta(hours=1)) == 0
        assert await linked.pins.get("user-alex") == before
        assert await linked.repo.revoke_all("user-nobody", KILL, T0) == 0
        assert await linked.pins.get("user-nobody") is None

    async def test_the_unlock_field_is_written_only_once_set(self, kit_db):
        repo, db = firestore_repo(kit_db)
        await repo.create(session())
        assert "facts_unlocked_until" not in db.collection(SESSIONS).docs["sid-1"]
        await repo.set_facts_unlock("sid-1", T0 + timedelta(minutes=15), T0)
        assert db.collection(SESSIONS).docs["sid-1"]["facts_unlocked_until"] == T0 + timedelta(minutes=15)
