"""MemoryInboxRepository kit (MCP_MEMORY_SUBMISSIONS_RFC §6): the docstring of `src/ports/memory_inbox_repository.py`
checked against `FirestoreMemoryInboxRepository` on the kits' database (`kit_db`: the evaluating fake with the index
refusal on, the test database in the cloud gate). Assertions are on the raw stored documents and on what the readers
return. The store is built once per test by `inbox_store` (`tests/support/memory_inbox_store.py`).
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence

import pytest

from src.domain.memory_inbox import (
    AddReceipt,
    AnswerResult,
    AppliedTriage,
    Basis,
    CandidateDraft,
    CandidateFields,
    CandidateStatus,
    DiscardReason,
    DraftClass,
    InboxState,
    Relation,
    RelationKind,
    SettledFinal,
    SubmissionDraft,
    SubmittingClient,
    TriageDecisions,
)
from tests.support.memory_inbox_store import GROUPS, INBOX, INDEXES_FILE, inbox_store

UTC = timezone.utc
AT = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
DAY = "2026-11-25"
CLIENT = SubmittingClient(client_id="c1", client_name="Desk assistant")
COIMBRA = "Clara booked the Coimbra weekend"
TRIAGE_TEXT = "Clara booked the Coimbra weekend, March 6-8"
DRAFT = DraftClass(directive=False, temporal_class="event", context_priority="medium", expiry={"kind": "none"})
NEW = Relation(kind=RelationKind.NEW, fact_id=None, fact_text=None)


@pytest.fixture
def store(kit_db):
    return inbox_store(kit_db(indexes=INDEXES_FILE, groups=GROUPS))


def draft(information: str = COIMBRA) -> SubmissionDraft:
    return SubmissionDraft(information=information, basis=Basis.SAID, said_when="", happens_when="March 6-8")


def triage_fields(text: str = TRIAGE_TEXT) -> CandidateFields:
    return CandidateFields(text=text, happens_when="March 6-8, 2027", said_when="late November 2026", basis=Basis.SAID)


def cites(*ids: str, text: str = TRIAGE_TEXT) -> CandidateDraft:
    return CandidateDraft(source_ids=tuple(ids), fields=triage_fields(text), draft=DRAFT, relation=NEW)


async def submit(store, *informations: str, at: datetime = AT, user: str = "u1") -> List[str]:
    """Store one submission per text, each one second after the previous; their ids in that order."""
    before = set(store.docs)
    for n, information in enumerate(informations):
        await store.repo.add_submissions(user, "a1", CLIENT, [draft(information)], at + timedelta(seconds=n), DAY)
    added = [i for i in store.docs if i not in before]
    return sorted(added, key=lambda i: store.docs[i]["received_at"])


async def triage(store, sent: Sequence[str], *candidates: CandidateDraft, discards: Dict[str, DiscardReason] = None,
                 at: datetime = AT, user: str = "u1") -> AppliedTriage:
    return await store.repo.apply_triage(user, "a1", sent, TriageDecisions(candidates=candidates,
                                                                           discards=dict(discards or {})), "en", at)


async def a_candidate(store, user: str = "u1") -> str:
    """One waiting candidate of `user`, made at AT from a submission of its own; its id."""
    [sid] = await submit(store, COIMBRA, user=user)
    before = set(store.docs)
    await triage(store, [sid], cites(sid), user=user)
    [cid] = [i for i in store.docs if i not in before]
    return cid


def assert_no_open_record_carries_purge_after(store) -> None:
    for doc in store.docs.values():
        if doc["status"] in ("received", "candidate", "confirmed"):
            assert "purge_after" not in doc, doc["status"]
        else:
            assert doc["purge_after"] is not None, doc["status"]


# ------------------------------------------------------------------------------------------------ submissions


async def test_add_submissions_stores_received_records_and_counts_the_owner_day(store):
    r = await store.repo.add_submissions("u1", "a1", CLIENT, [draft()] * 3, AT, DAY)
    assert r == AddReceipt(stored=3, over_day=0)
    docs = list(store.docs.values())
    assert {d["status"] for d in docs} == {"received"} and all("purge_after" not in d for d in docs)
    assert all(d["user_id"] == "u1" and d["account_id"] == "a1" and d["client_id"] == "c1"
               and d["client_name"] == "Desk assistant" and d["information"] == COIMBRA and d["basis"] == "said"
               and d["happens_when"] == "March 6-8" and d["received_at"] == AT and d["triage_offers"] == 0
               for d in docs)
    assert (await store.repo.state("u1")).received_day == (DAY, 3)


async def test_the_day_cap_stores_up_to_one_hundred_and_reports_the_rest(store):
    assert await store.repo.add_submissions("u1", "a1", CLIENT, [draft()] * 98, AT, DAY) == AddReceipt(98, 0)
    assert await store.repo.add_submissions("u1", "a1", CLIENT, [draft()] * 5, AT, DAY) == AddReceipt(2, 3)
    assert await store.repo.add_submissions("u1", "a1", CLIENT, [draft()], AT, DAY) == AddReceipt(0, 1)
    assert len(store.docs) == 100 and (await store.repo.state("u1")).received_day == (DAY, 100)


async def test_a_new_owner_day_restarts_the_count(store):
    await store.repo.add_submissions("u1", "a1", CLIENT, [draft()] * 100, AT, DAY)
    r = await store.repo.add_submissions("u1", "a1", CLIENT, [draft()] * 5, AT + timedelta(hours=20), "2026-11-26")
    assert r == AddReceipt(stored=5, over_day=0)
    assert (await store.repo.state("u1")).received_day == ("2026-11-26", 5)


async def test_received_is_oldest_first_and_limited(store):
    ids = await submit(store, *[f"Alex trains with Robin at the Iron Temple gym, note {n}" for n in range(45)])
    await submit(store, COIMBRA, user="u2")
    got = await store.repo.received("u1", 40)
    assert [s.submission_id for s in got] == ids[:40]
    assert [s.received_at for s in got] == [AT + timedelta(seconds=n) for n in range(40)]


# ------------------------------------------------------------------------------------------------ triage


async def test_apply_triage_settles_cited_discarded_and_undecided(store):
    s1, s2, s3 = await submit(store, COIMBRA, "Maria likes tea", "Something about the weather")
    later = AT + timedelta(days=1)

    applied = await triage(store, [s1, s2, s3], cites(s1), discards={s2: DiscardReason.KNOWN}, at=later)

    assert applied == AppliedTriage(candidates=1, triaged=1, discarded=1, offered=1, undecided=0)
    [(cid, cand)] = store.of_status("candidate").items()
    assert cand["answer_by"] == later + timedelta(days=30) and cand["created_at"] == later and cand["language"] == "en"
    assert cand["source_ids"] == [s1] and cand["text"] == TRIAGE_TEXT and cand["relation"]["kind"] == "new"
    assert [(s["submission_id"], s["information"], s["received_at"]) for s in cand["sources"]] == [(s1, COIMBRA, AT)]
    assert store.docs[s1]["status"] == "triaged" and store.docs[s1]["purge_after"] == later + timedelta(days=30)
    assert store.docs[s1]["triaged_at"] == later
    assert store.docs[s2]["status"] == "discarded" and store.docs[s2]["discard_reason"] == "known"
    assert store.docs[s3]["status"] == "received" and store.docs[s3]["triage_offers"] == 1
    assert "purge_after" not in store.docs[s3]

    again = await triage(store, [s3], at=later + timedelta(days=1))

    assert again == AppliedTriage(candidates=0, triaged=0, discarded=0, offered=0, undecided=1)
    assert store.docs[s3]["status"] == "discarded" and store.docs[s3]["discard_reason"] == "undecided"
    assert store.docs[s3]["triage_offers"] == 2 and store.docs[s3]["purge_after"] == later + timedelta(days=31)
    assert await store.repo.received("u1", 40) == []


async def test_apply_triage_leaves_a_submission_that_is_no_longer_received(store):
    s1, s2 = await submit(store, COIMBRA, "Maria likes tea")
    other = (await submit(store, "Dr. Marta Quillen moved the appointment", user="u2"))[0]
    await triage(store, [s1], cites(s1))
    settled = dict(store.docs[s1])

    applied = await triage(store, [s1, s2, other, "missing"], cites(s1, text="a second card"),
                           cites(other, text="another user's card"), discards={s1: DiscardReason.TRIVIAL})

    assert applied == AppliedTriage(candidates=0, triaged=0, discarded=0, offered=1, undecided=0)
    assert store.docs[s1] == settled
    assert store.docs[other]["status"] == "received" and store.docs[other]["triage_offers"] == 0
    assert len(store.of_status("candidate")) == 1


async def test_a_submission_both_cited_and_discarded_is_cited(store):
    s1, s2 = await submit(store, COIMBRA, "Alex goes with Clara")

    applied = await triage(store, [s1, s2], cites(s1, s2), cites(s2, text="Alex goes with Clara"),
                           discards={s2: DiscardReason.WAITING})

    assert applied == AppliedTriage(candidates=2, triaged=2, discarded=0, offered=0, undecided=0)
    assert store.docs[s2]["status"] == "triaged" and store.docs[s2]["discard_reason"] is None
    split = sorted(len(c["sources"]) for c in store.of_status("candidate").values())
    assert split == [1, 2]


# ------------------------------------------------------------------------------------------------ the owner's answers


async def test_confirm_keeps_triages_fields_in_proposed_and_sets_final_due(store):
    cid = await a_candidate(store)
    edited_fields = CandidateFields(text="Clara booked the Coimbra weekend, March 6-8, with Alex",
                                    happens_when="March 6-8, 2027", said_when="November 2026", basis=Basis.DOCUMENT)

    assert await store.repo.confirm(cid, "u1", edited_fields, AT) is AnswerResult.DONE

    doc = store.docs[cid]
    assert doc["status"] == "confirmed" and doc["edited"] is True and doc["proposed"]["text"] == TRIAGE_TEXT
    assert doc["text"] == edited_fields.text and doc["basis"] == "document" and doc["said_when"] == "November 2026"
    assert doc["confirmed_at"] == AT and "purge_after" not in doc
    assert (await store.repo.state("u1")).final_due_at == AT + timedelta(hours=1)


async def test_a_new_confirmation_restarts_the_final_retry_count(store):
    first, second = await a_candidate(store), await a_candidate(store)
    await store.repo.confirm(first, "u1", None, AT)
    for _ in range(3):
        await store.repo.bump_final_retry("u1")
    assert (await store.repo.state("u1")).final_attempts == 3

    await store.repo.confirm(second, "u1", None, AT + timedelta(hours=5))

    state = await store.repo.state("u1")
    assert state.final_attempts == 0 and state.final_due_at == AT + timedelta(hours=6)


async def test_confirm_without_fields_or_with_triages_own_is_not_edited(store):
    first, second = await a_candidate(store), await a_candidate(store)

    assert await store.repo.confirm(first, "u1", None, AT) is AnswerResult.DONE
    assert await store.repo.confirm(second, "u1", triage_fields(), AT + timedelta(minutes=5)) is AnswerResult.DONE

    for cid in (first, second):
        assert store.docs[cid]["edited"] is False and store.docs[cid]["text"] == TRIAGE_TEXT
        assert store.docs[cid]["proposed"] == triage_fields().to_record()
    assert (await store.repo.state("u1")).final_due_at == AT + timedelta(minutes=65)


async def test_confirm_and_reject_refuse_unknown_foreign_and_answered(store):
    mine, theirs = await a_candidate(store), await a_candidate(store, user="u2")
    [submission] = await submit(store, "Maria likes tea")
    assert await store.repo.reject(mine, "u1", AT) is AnswerResult.DONE
    before = {i: dict(d) for i, d in store.docs.items()}

    for answer in (lambda cid: store.repo.confirm(cid, "u1", triage_fields("x"), AT),
                   lambda cid: store.repo.reject(cid, "u1", AT)):
        assert await answer("missing") is AnswerResult.NOT_FOUND
        assert await answer(submission) is AnswerResult.NOT_FOUND
        assert await answer(theirs) is AnswerResult.OTHER_USER
        assert await answer(mine) is AnswerResult.NOT_WAITING

    assert {i: dict(d) for i, d in store.docs.items()} == before
    assert store.docs[mine]["status"] == "rejected" and store.docs[mine]["purge_after"] == AT + timedelta(days=180)
    assert store.docs[mine]["rejected_at"] == AT
    assert (await store.repo.state("u1")).final_due_at is None


async def test_an_expired_candidate_is_not_confirmed_and_a_confirmed_one_is_not_expired(store):   # Review Focus 1
    # order A: the sweep's expiry first, then the owner's "True"
    expired = await a_candidate(store)
    answer_by = store.docs[expired]["answer_by"]
    assert await store.repo.expire_due(answer_by) == 1
    assert await store.repo.confirm(expired, "u1", None, answer_by) is AnswerResult.NOT_WAITING
    doc = store.docs[expired]
    assert doc["status"] == "expired" and doc["purge_after"] == answer_by + timedelta(days=30)
    assert doc["confirmed_at"] is None

    # order B: the owner's "True", then the sweep's expiry past `answer_by`
    confirmed = await a_candidate(store)
    assert await store.repo.confirm(confirmed, "u1", None, AT) is AnswerResult.DONE
    assert await store.repo.expire_due(answer_by + timedelta(seconds=1)) == 0
    assert store.docs[confirmed]["status"] == "confirmed" and "purge_after" not in store.docs[confirmed]


@pytest.mark.h1_only("forces a concurrent confirmation with interfere_on_txn_read")
async def test_a_confirmation_committed_during_the_expiry_read_wins(store):              # Review Focus 1
    cid = await a_candidate(store)
    answer_by = store.docs[cid]["answer_by"]
    store.db.interfere_on_txn_read(INBOX, cid, {"status": "confirmed", "confirmed_at": AT}, after_read=True)

    assert await store.repo.expire_due(answer_by) == 0

    assert store.docs[cid]["status"] == "confirmed" and "purge_after" not in store.docs[cid]


async def test_only_due_waiting_candidates_expire(store):
    due, later = await a_candidate(store), await a_candidate(store)
    store.docs[later] = {**store.docs[later], "answer_by": AT + timedelta(days=40)}

    assert await store.repo.expire_due(AT + timedelta(days=30)) == 1

    assert store.docs[due]["status"] == "expired" and store.docs[later]["status"] == "candidate"
    assert [c.candidate_id for c in await store.repo.candidates("u1", {CandidateStatus.CANDIDATE})] == [later]


# ------------------------------------------------------------------------------------------------ final pass


async def test_settle_final_marks_done_or_counts_and_gives_up_at_three(store):
    done, failing = await a_candidate(store), await a_candidate(store)
    for cid in (done, failing):
        await store.repo.confirm(cid, "u1", None, AT)

    assert await store.repo.settle_final([done], True, AT) == SettledFinal(consolidated=1, retried=0, gave_up=())
    assert store.docs[done]["status"] == "consolidated" and store.docs[done]["outcome"] == "done"
    assert store.docs[done]["consolidated_at"] == AT and store.docs[done]["purge_after"] == AT + timedelta(days=180)

    assert await store.repo.settle_final([failing], False, AT) == SettledFinal(0, 1, ())
    assert await store.repo.settle_final([failing], False, AT) == SettledFinal(0, 1, ())
    assert store.docs[failing]["status"] == "confirmed" and store.docs[failing]["final_attempts"] == 2
    assert await store.repo.settle_final([failing], False, AT) == SettledFinal(0, 0, (failing,))
    assert store.docs[failing]["status"] == "consolidated" and store.docs[failing]["outcome"] == "gave_up"
    assert store.docs[failing]["final_attempts"] == 3


async def test_settle_final_touches_only_candidates_still_confirmed(store):              # Review Focus 3
    sent, late, waiting = await a_candidate(store), await a_candidate(store), await a_candidate(store)
    await store.repo.confirm(sent, "u1", None, AT)
    await store.repo.settle_final([sent], True, AT)
    settled = dict(store.docs[sent])

    applied = await store.repo.settle_final([sent, waiting, "missing"], True, AT + timedelta(hours=1))
    await store.repo.confirm(late, "u1", None, AT + timedelta(hours=2))      # confirmed after the pass read its batch

    assert applied == SettledFinal(consolidated=0, retried=0, gave_up=())
    assert store.docs[sent] == settled and store.docs[waiting]["status"] == "candidate"
    assert store.docs[late]["status"] == "confirmed"


async def test_clear_final_due_only_when_it_is_still_the_value_read(store):
    first, second = await a_candidate(store), await a_candidate(store)
    await store.repo.confirm(first, "u1", None, AT)
    read = (await store.repo.state("u1")).final_due_at
    assert await store.repo.bump_final_retry("u1") == 1
    await store.repo.confirm(second, "u1", None, AT + timedelta(minutes=10))    # a confirmation during the run
    assert await store.repo.bump_final_retry("u1") == 1                         # the confirmation restarted the count

    assert await store.repo.clear_final_due("u1", read, AT + timedelta(hours=1)) is False
    state = await store.repo.state("u1")
    assert state.final_due_at == AT + timedelta(minutes=70) and state.final_attempts == 1
    assert state.last_final_at == AT + timedelta(hours=1)

    assert await store.repo.clear_final_due("u1", state.final_due_at, AT + timedelta(hours=2)) is True
    state = await store.repo.state("u1")
    assert (state.final_due_at, state.final_attempts, state.last_final_at) == (None, 0, AT + timedelta(hours=2))


# ------------------------------------------------------------------------------------------------ sweep and state


async def test_sweep_reads_received_users_and_overdue_finals(store):
    early, late = await a_candidate(store, "u1"), await a_candidate(store, "u3")      # their submissions are triaged
    await submit(store, COIMBRA, "Maria likes tea")
    await submit(store, "Dr. Marta Quillen moved the appointment", user="u2")
    await store.repo.confirm(early, "u1", None, AT)                       # final due at AT + 1 h
    await store.repo.confirm(late, "u3", None, AT + timedelta(hours=3))   # final due at AT + 4 h

    assert sorted(await store.repo.users_with_received()) == [("u1", "a1"), ("u2", "a1")]
    assert await store.repo.users_final_overdue(AT + timedelta(hours=2)) == ["u1"]


async def test_the_state_records_triage_runs_and_starts_empty(store):
    assert await store.repo.state("u1") == InboxState(user_id="u1")
    await store.repo.add_submissions("u1", "a1", CLIENT, [draft()], AT, DAY)

    await store.repo.record_triage("u1", True, AT)
    await store.repo.record_triage("u1", False, AT + timedelta(days=1))

    state = await store.repo.state("u1")
    assert (state.last_triage_at, state.last_triage_failed_at, state.received_day) == \
           (AT, AT + timedelta(days=1), (DAY, 1))
    assert all("expires_at" not in d for d in list(store.docs.values()) + list(store.state_docs.values()))


async def test_no_open_record_ever_carries_purge_after(store):
    s1, s2, s3, s4 = await submit(store, COIMBRA, "Maria likes tea", "Alex trains with Robin", "unclear")
    assert_no_open_record_carries_purge_after(store)
    await triage(store, [s1, s2, s3, s4], cites(s1), cites(s2, text="Maria likes tea"),
                 cites(s3, text="Alex trains with Robin"), discards={s4: DiscardReason.UNCLEAR})
    assert_no_open_record_carries_purge_after(store)
    confirmed, rejected, waiting = sorted(store.of_status("candidate"))
    await store.repo.confirm(confirmed, "u1", None, AT)
    await store.repo.reject(rejected, "u1", AT)
    assert_no_open_record_carries_purge_after(store)
    await store.repo.settle_final([confirmed], False, AT)
    assert_no_open_record_carries_purge_after(store)
    await store.repo.expire_due(AT + timedelta(days=30))
    await store.repo.settle_final([confirmed], True, AT)
    assert_no_open_record_carries_purge_after(store)
    assert {store.docs[i]["status"] for i in (confirmed, rejected, waiting)} == {"consolidated", "rejected", "expired"}
