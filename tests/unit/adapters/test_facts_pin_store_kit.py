"""FirestoreFactsPinRepository over the kit backend (`kit_db`: `EvalDb` with index refusal in the unit suite, the test
database in `make test-cloud`), and the port's rules run on both it and the in-memory twin
(`tests/support/facts_pin_repo.py`) so the two cannot drift (CABINET_DEVICE_SESSIONS_RFC §7.7, §7.8): `set_codes` by
`replace`, `clear`, every attempt outcome and exactly its writes (the counter on the record, the lock flag on the user
document, in one transaction), parallel wrong attempts never passing five, the first pending reset kept, a reset applied
only when due. Firestore-only cases assert the raw stored documents, that a code never reaches them, that the user
document the lock wrote reads back as a locked `UserProfile`, and that a document contradicting its id is absent."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.firestore_facts_pin_repo import FirestoreFactsPinRepository
from src.domain.facts_pin import FactsPin
from src.domain.facts_pin_policy import FactsPinPolicy as P, PinOutcome, PinVerdict
from src.domain.secret_digest import keyed_digest
from src.domain.user import InactiveReason, UserProfile
from tests.support.facts_pin_repo import FakeFactsPinRepository
from tests.support.firestore_eval import EvalDocRef

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
PINS = "eval_facts_pin_v1"
USERS = "eval_domain_users_v2"
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
KEY = bytes(range(32))


def pin_d(code: str, user_id: str = "user-alex") -> str:
    return keyed_digest(KEY, "facts_pin", f"{user_id}:{code}")


def duress_d(code: str, user_id: str = "user-alex") -> str:
    return keyed_digest(KEY, "duress_pin", f"{user_id}:{code}")


def attempt(code: str):
    """The two digests an attempt with `code` presents."""
    return pin_d(code), duress_d(code)


class Store:
    """One backend: the repository, how a user document is added, and how the user's flag reads back."""

    def __init__(self, kind: str, kit_db) -> None:
        self.kind = kind
        if kind == "firestore":
            self.db = kit_db(indexes=INDEXES_FILE, groups={PINS: "development_facts_pin_v1",
                                                           USERS: "development_domain_users_v2"})
            self.repo = FirestoreFactsPinRepository(self.db, PINS, USERS)
        else:
            self.repo = FakeFactsPinRepository()

    def add_user(self, user_id: str = "user-alex", *, is_active: bool = True) -> None:
        if self.kind == "firestore":
            self.db.collection(USERS).docs[user_id] = {"user_id": user_id, "email": "alex@example.com",
                                                        "is_active": is_active}
        else:
            self.repo.add_user(user_id, is_active=is_active)

    def flag(self, user_id: str = "user-alex"):
        """(is_active, inactive_reason) as stored on the user."""
        if self.kind == "firestore":
            doc = self.db.collection(USERS).docs[user_id]
        else:
            doc = self.repo.users[user_id]
        return doc["is_active"], doc.get("inactive_reason")

    async def with_pin(self, wrong: int = 0, user_id: str = "user-alex") -> None:
        self.add_user(user_id)
        assert await self.repo.set_codes(user_id, pin_d("0001", user_id), duress_d("0002", user_id), T0,
                                         replace=False)
        for n in range(wrong):
            await self.repo.check(user_id, *attempt("0000"), T0 + timedelta(seconds=n))


@pytest.fixture(params=["firestore", "fake"])
def store(request, kit_db):
    return Store(request.param, kit_db)


def stored(**over) -> FactsPin:
    return FactsPin(**{"user_id": "user-alex", "pin_digest": pin_d("0001"), "duress_digest": duress_d("0002"),
                       "updated_at": T0, **over})


# ---------------------------------------------------------------------------------------------- the port's rules

class TestCodes:
    async def test_a_first_pin_is_stored_and_reads_back(self, store):
        assert await store.repo.get("user-alex") is None
        assert await store.repo.set_codes("user-alex", pin_d("0001"), duress_d("0002"), T0, replace=False) is True
        assert await store.repo.get("user-alex") == stored()

    async def test_a_first_pin_never_overwrites_and_a_change_needs_a_record(self, store):
        assert await store.repo.set_codes("user-alex", pin_d("0002"), duress_d("0001"), T0, replace=True) is False
        assert await store.repo.get("user-alex") is None
        await store.with_pin()
        assert await store.repo.set_codes("user-alex", pin_d("0002"), duress_d("0001"), T0, replace=False) is False
        assert await store.repo.get("user-alex") == stored()

    async def test_a_change_replaces_both_digests_and_clears_the_counter_and_the_reset(self, store):
        await store.with_pin(wrong=2)
        await store.repo.request_reset("user-alex", T0 + timedelta(hours=1))
        later = T0 + timedelta(hours=2)
        assert await store.repo.set_codes("user-alex", pin_d("0002"), duress_d("0001"), later, replace=True) is True
        assert await store.repo.get("user-alex") == FactsPin(user_id="user-alex", pin_digest=pin_d("0002"),
                                                             duress_digest=duress_d("0001"), updated_at=later)

    async def test_clear_deletes_the_record_once(self, store):
        await store.with_pin()
        assert await store.repo.clear("user-alex") is True
        assert await store.repo.get("user-alex") is None
        assert await store.repo.clear("user-alex") is False


class TestAttempts:
    async def test_without_a_record_nothing_is_counted_or_locked(self, store):
        store.add_user()
        assert await store.repo.check("user-alex", *attempt("0001"), T0) == PinOutcome(PinVerdict.NO_PIN)
        assert await store.repo.get("user-alex") is None
        assert store.flag() == (True, None)

    async def test_the_pin_answers_ok_and_clears_the_counter(self, store):
        await store.with_pin(wrong=3)
        now = T0 + timedelta(hours=1)
        assert await store.repo.check("user-alex", *attempt("0001"), now) == PinOutcome(PinVerdict.OK)
        assert await store.repo.get("user-alex") == stored(updated_at=now)
        assert store.flag() == (True, None)

    async def test_the_pin_on_a_clear_counter_writes_nothing(self, store):
        await store.with_pin()
        assert await store.repo.check("user-alex", *attempt("0001"), T0 + timedelta(hours=1)) == PinOutcome(
            PinVerdict.OK)
        assert await store.repo.get("user-alex") == stored()

    async def test_wrong_codes_count_down_and_the_fifth_locks_the_account(self, store):
        await store.with_pin()
        outcomes = []
        for n in range(5):
            outcomes.append(await store.repo.check("user-alex", *attempt("0000"), T0 + timedelta(minutes=n)))
        assert outcomes == [PinOutcome(PinVerdict.WRONG, attempts_left=left) for left in (4, 3, 2, 1)] + [
            PinOutcome(PinVerdict.LOCKED, locked_now=True)]
        assert await store.repo.get("user-alex") == stored(wrong_in_row=5, updated_at=T0 + timedelta(minutes=4))
        assert store.flag() == (False, "pin_attempts")

    async def test_the_duress_code_locks_the_account_and_leaves_the_record(self, store):
        await store.with_pin(wrong=2)
        before = await store.repo.get("user-alex")
        assert await store.repo.check("user-alex", *attempt("0002"), T0 + timedelta(hours=1)) == PinOutcome(
            PinVerdict.DURESS, locked_now=True)
        assert await store.repo.get("user-alex") == before
        assert store.flag() == (False, "duress")

    @pytest.mark.parametrize("code", ["0001", "0002", "0000"])
    async def test_a_locked_account_answers_locked_and_nothing_is_written(self, store, code):
        await store.with_pin()
        await store.repo.check("user-alex", *attempt("0002"), T0)                    # locked by the duress code
        before = await store.repo.get("user-alex")
        assert await store.repo.check("user-alex", *attempt(code), T0 + timedelta(hours=1)) == PinOutcome(
            PinVerdict.LOCKED)
        assert await store.repo.get("user-alex") == before
        assert store.flag() == (False, "duress")

    async def test_a_user_set_inactive_by_hand_is_locked_and_keeps_no_reason(self, store):
        await store.with_pin()
        store.add_user(is_active=False)
        assert await store.repo.check("user-alex", *attempt("0000"), T0) == PinOutcome(PinVerdict.LOCKED)
        assert (await store.repo.get("user-alex")).wrong_in_row == 0
        assert store.flag() == (False, None)

    async def test_digests_of_another_user_are_wrong_codes(self, store):
        await store.with_pin()
        outcome = await store.repo.check("user-alex", pin_d("0001", "user-other"), duress_d("0002", "user-other"), T0)
        assert outcome == PinOutcome(PinVerdict.WRONG, attempts_left=4)

    async def test_parallel_wrong_attempts_never_pass_five(self, store):
        await store.with_pin(wrong=2)
        if store.kind == "firestore":
            store.repo._col.yield_on_get = True      # every transaction reads the record before any commits
        results = await asyncio.gather(*(store.repo.check("user-alex", *attempt("0000"), T0 + timedelta(hours=1))
                                         for _ in range(4)))
        verdicts = sorted((r.verdict.value, r.attempts_left, r.locked_now) for r in results)
        assert verdicts == [("locked", None, False), ("locked", None, True), ("wrong", 1, False),
                            ("wrong", 2, False)]
        assert (await store.repo.get("user-alex")).wrong_in_row == 5
        assert store.flag() == (False, "pin_attempts")


class TestReset:
    async def test_a_reset_is_pending_for_a_day_and_the_first_request_is_kept(self, store):
        await store.with_pin()
        asked, again = T0 + timedelta(hours=1), T0 + timedelta(hours=3)
        assert await store.repo.request_reset("user-alex", asked) is True
        assert await store.repo.request_reset("user-alex", again) is False
        assert await store.repo.get("user-alex") == stored(reset_requested_at=asked, reset_due_at=P.reset_due(asked),
                                                           updated_at=asked)

    async def test_a_reset_needs_a_record(self, store):
        assert await store.repo.request_reset("user-alex", T0) is False
        assert await store.repo.cancel_reset("user-alex", T0) is False
        assert await store.repo.apply_reset("user-alex", T0 + timedelta(days=2)) is False
        assert await store.repo.get("user-alex") is None

    async def test_cancel_clears_a_pending_reset_once(self, store):
        await store.with_pin()
        await store.repo.request_reset("user-alex", T0)
        cancelled = T0 + timedelta(hours=2)
        assert await store.repo.cancel_reset("user-alex", cancelled) is True
        assert await store.repo.cancel_reset("user-alex", cancelled) is False
        assert await store.repo.get("user-alex") == stored(updated_at=cancelled)

    async def test_apply_deletes_the_record_only_once_due(self, store):
        await store.with_pin()
        await store.repo.request_reset("user-alex", T0)
        due = P.reset_due(T0)
        assert await store.repo.apply_reset("user-alex", due - timedelta(microseconds=1)) is False
        assert (await store.repo.get("user-alex")).reset_due_at == due
        assert await store.repo.apply_reset("user-alex", due) is True
        assert await store.repo.get("user-alex") is None

    async def test_apply_without_a_pending_reset_keeps_the_record(self, store):
        await store.with_pin()
        assert await store.repo.apply_reset("user-alex", T0 + timedelta(days=3)) is False
        assert await store.repo.get("user-alex") == stored()


# ---------------------------------------------------------------------------------------------- Firestore only

class TestFirestoreDocuments:
    async def test_the_stored_document_holds_digests_and_never_a_code(self, kit_db):
        store = Store("firestore", kit_db)
        await store.with_pin(wrong=1)
        await store.repo.request_reset("user-alex", T0 + timedelta(hours=1))
        doc = store.db.collection(PINS).docs["user-alex"]
        assert set(doc) == {"user_id", "pin_digest", "duress_digest", "wrong_in_row", "reset_requested_at",
                            "reset_due_at", "updated_at"}
        assert (doc["pin_digest"], doc["duress_digest"], doc["wrong_in_row"]) == (pin_d("0001"), duress_d("0002"), 1)
        assert doc["reset_due_at"] == T0 + timedelta(hours=25)
        assert "0001" not in str(doc) and "0002" not in str(doc)

    async def test_the_lock_reads_back_as_a_locked_profile_and_touches_nothing_else(self, kit_db):
        store = Store("firestore", kit_db)
        await store.with_pin()
        await store.repo.check("user-alex", *attempt("0002"), T0 + timedelta(hours=1))
        doc = dict(store.db.collection(USERS).docs["user-alex"])
        assert doc["email"] == "alex@example.com" and doc["updated_at"] == T0 + timedelta(hours=1)
        profile = UserProfile(**doc)
        assert (profile.is_active, profile.inactive_reason) == (False, InactiveReason.DURESS)

    @pytest.mark.h1_only("counts the fake's transactional EvalDocRef reads to prove the attempts interleaved")
    async def test_parallel_attempts_conflict_and_rerun_against_the_new_count(self, kit_db, monkeypatch):
        store = Store("firestore", kit_db)
        await store.with_pin(wrong=2)
        reads = []
        original = EvalDocRef.get

        async def counted(ref, transaction=None, field_paths=None):
            if transaction is not None and ref.col.name == PINS:
                reads.append(ref.id)
            return await original(ref, transaction=transaction, field_paths=field_paths)

        monkeypatch.setattr(EvalDocRef, "get", counted)
        store.repo._col.yield_on_get = True
        results = await asyncio.gather(*(store.repo.check("user-alex", *attempt("0000"), T0) for _ in range(4)))
        assert len(reads) == 4 + 3 + 2 + 1          # each round all remaining attempts read, one commits, the rest re-run
        assert sum(r.locked_now for r in results) == 1
        assert (await store.repo.get("user-alex")).wrong_in_row == 5

    async def test_an_absent_user_document_reads_as_locked(self, kit_db):
        store = Store("firestore", kit_db)
        await store.repo.set_codes("user-alex", pin_d("0001"), duress_d("0002"), T0, replace=False)
        assert await store.repo.check("user-alex", *attempt("0000"), T0) == PinOutcome(PinVerdict.LOCKED)
        assert store.db.collection(USERS).docs == {}

    async def test_a_document_contradicting_its_id_is_absent_everywhere(self, kit_db, caplog):
        store = Store("firestore", kit_db)
        await store.with_pin()
        store.add_user("user-forged")
        store.db.collection(PINS).docs["user-forged"] = dict(store.db.collection(PINS).docs["user-alex"])
        repo = store.repo
        assert await repo.get("user-forged") is None
        assert await repo.check("user-forged", *attempt("0002"), T0) == PinOutcome(PinVerdict.NO_PIN)
        assert store.flag("user-forged") == (True, None)
        assert await repo.set_codes("user-forged", pin_d("0002"), duress_d("0001"), T0, replace=True) is False
        assert await repo.request_reset("user-forged", T0) is False
        assert await repo.clear("user-forged") is False
        assert store.db.collection(PINS).docs["user-forged"]["user_id"] == "user-alex"
        assert "does not match its stored user_id" in caplog.text
