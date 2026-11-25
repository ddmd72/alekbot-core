"""The Cabinet's memory-inbox routes (MCP_MEMORY_SUBMISSIONS_RFC §8.2) over the real `MemoryInboxService` on the real inbox
store (the evaluating fake, `tests/support/memory_inbox_store.py`) and the real Cloud Tasks queue adapter over a recording
client. Assertions are on the answers and on what the store holds. Callers come from the invented cast: usr-1 Alex
(acc-1), usr-2 Clara (acc-2)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from quart import Quart
from unittest.mock import MagicMock

from src.composition.connector_registry import ConnectorRegistry
from src.domain.memory_inbox import (
    Basis,
    CandidateDraft,
    CandidateFields,
    DraftClass,
    Relation,
    RelationKind,
    SubmissionDraft,
    SubmittingClient,
    TriageDecisions,
)
from src.services.memory_inbox_service import MemoryInboxService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_session_kit import cabinet_session_for
from tests.support.memory_inbox_store import inbox_store
from tests.support.recording_task_queue import make_task_queue

UTC = timezone.utc
NOW = datetime(2026, 11, 25, 11, 30, tzinfo=UTC)
CLIENT = SubmittingClient(client_id="c1", client_name="Desk assistant")
TRIAGE_TEXT = "Clara booked the Coimbra weekend"
DRAFT = DraftClass(directive=False, temporal_class="event", context_priority="medium", expiry={"kind": "none"})
BASE = "/api/user/memory-inbox"


class LockedPin:
    async def facts_open(self, user_id: str, account_id: str, session_id: str) -> bool:
        return False


class FailingQueue:
    async def enqueue_worker_task(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Cloud Tasks unavailable")


class World:
    def __init__(self, queue: Any = None, facts_pin: Any = None, user: str = "usr-1", account: str = "acc-1") -> None:
        self.inbox = inbox_store()
        self.queue, self.tasks = make_task_queue()
        service = MemoryInboxService(store=self.inbox.repo, task_queue=queue or self.queue, owner_zones=SimpleNamespace(),
                                     clock=lambda: NOW)
        verifier, self.headers = cabinet_session_for(user, account)
        self.app = Quart("test_cabinet_memory_inbox")
        self.app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), cabinet_sessions=verifier, user_repo=MagicMock(), fact_repo=MagicMock(),
            embedding_service=MagicMock(), connectors=ConnectorRegistry(), facts_pin=facts_pin, memory_inbox=service))
        self.client = self.app.test_client()

    async def candidate(self, text: str = TRIAGE_TEXT, user: str = "usr-1", account: str = "acc-1",
                        at: datetime = NOW, relation: Optional[Relation] = None) -> str:
        await self.inbox.repo.add_submissions(user, account, CLIENT, [SubmissionDraft(text, Basis.SAID, "", "")], at,
                                              "2026-11-26")
        [sid] = [s.submission_id for s in await self.inbox.repo.received(user, 40)]
        before = set(self.inbox.docs)
        fields = CandidateFields(text=text, happens_when="March 6-8", said_when="", basis=Basis.SAID)
        await self.inbox.repo.apply_triage(user, account, [sid], TriageDecisions(candidates=(CandidateDraft(
            source_ids=(sid,), fields=fields, draft=DRAFT,
            relation=relation or Relation(RelationKind.NEW, None, None)),)), "en", at)
        [cid] = [i for i in self.inbox.docs if i not in before]
        return cid

    def doc(self, candidate_id: str) -> dict:
        return self.inbox.docs[candidate_id]

    async def get(self, path: str = BASE):
        return await self.client.get(path, headers=self.headers)

    async def post(self, path: str, body: Optional[dict] = None):
        return await self.client.post(path, json=body if body is not None else {}, headers=self.headers)


async def test_the_list_is_the_session_users_waiting_candidates_newest_first_with_the_confirmed_count():
    world = World()
    older = await world.candidate("Maria likes tea", at=NOW - timedelta(days=2))
    newer = await world.candidate(TRIAGE_TEXT, at=NOW - timedelta(hours=1),
                                  relation=Relation(RelationKind.REFINES, "f1", "Clara has a trip in March"))
    done = await world.candidate("Alex trains with Robin", at=NOW - timedelta(days=1))
    await world.post(f"{BASE}/{done}/confirm")
    await world.candidate("Clara's private note", user="usr-2", account="acc-2")

    resp = await world.get()
    body = await resp.get_json()

    assert resp.status_code == 200 and body["confirmed"] == 1
    assert [c["candidate_id"] for c in body["candidates"]] == [newer, older]
    card = body["candidates"][0]
    assert card["text"] == TRIAGE_TEXT and card["happens_when"] == "March 6-8" and card["basis"] == "said"
    assert card["source"] == "Desk assistant" and card["received"] == (NOW - timedelta(hours=1)).isoformat()
    assert card["draft"] == {"directive": False, "temporal_class": "event", "context_priority": "medium",
                             "expiry": {"kind": "none"}}
    assert card["relation"] == {"kind": "refines", "fact_text": "Clara has a trip in March"}
    assert card["raw"] == [{"information": TRIAGE_TEXT, "basis": "said", "said_when": "", "happens_when": "",
                            "received": (NOW - timedelta(hours=1)).isoformat()}]


async def test_confirm_stores_the_owners_edit_and_keeps_triages_text():
    world = World()
    cid = await world.candidate()

    resp = await world.post(f"{BASE}/{cid}/confirm", {"text": "Clara booked the Coimbra weekend, March 6-8"})

    assert resp.status_code == 200 and await resp.get_json() == {"status": "confirmed"}
    doc = world.doc(cid)
    assert doc["status"] == "confirmed" and doc["text"] == "Clara booked the Coimbra weekend, March 6-8"
    assert doc["proposed"]["text"] == TRIAGE_TEXT and doc["edited"] is True
    assert [name for name, _ in world.tasks.worker_tasks()] == ["memory_inbox_final"]


async def test_confirm_without_a_body_keeps_triages_fields():
    world = World()
    cid = await world.candidate()

    resp = await world.client.post(f"{BASE}/{cid}/confirm", headers=world.headers)

    assert resp.status_code == 200
    assert world.doc(cid)["status"] == "confirmed" and world.doc(cid)["edited"] is False


@pytest.mark.parametrize("body, error", [({"text": " ".join(["w"] * 61)}, "too_long"), ({"text": ""}, "empty_text"),
                                         ({"basis": "heard"}, "basis")])
async def test_a_bad_edit_is_400_and_writes_nothing(body, error):
    world = World()
    cid = await world.candidate()
    before = dict(world.doc(cid))

    resp = await world.post(f"{BASE}/{cid}/confirm", body)

    assert resp.status_code == 400 and await resp.get_json() == {"error": error}
    assert world.doc(cid) == before and world.tasks.worker_tasks() == []


async def test_another_users_candidate_is_403_unknown_404_answered_409():
    world = World()
    theirs = await world.candidate(user="usr-2", account="acc-2")
    mine = await world.candidate()
    await world.post(f"{BASE}/{mine}/reject")

    for verb in ("confirm", "reject"):
        other = await world.post(f"{BASE}/{theirs}/{verb}")
        unknown = await world.post(f"{BASE}/nope/{verb}")
        answered = await world.post(f"{BASE}/{mine}/{verb}")
        assert (other.status_code, await other.get_json()) == (403, {"error": "other_user"})
        assert (unknown.status_code, await unknown.get_json()) == (404, {"error": "not_found"})
        assert (answered.status_code, await answered.get_json()) == (409, {"error": "not_waiting"})
    assert world.doc(theirs)["status"] == "candidate"


async def test_reject_marks_rejected_with_its_retention():
    world = World()
    cid = await world.candidate()

    resp = await world.post(f"{BASE}/{cid}/reject")

    assert resp.status_code == 200 and await resp.get_json() == {"status": "rejected"}
    doc = world.doc(cid)
    assert doc["status"] == "rejected" and doc["purge_after"] == NOW + timedelta(days=180)
    assert world.tasks.worker_tasks() == []


async def test_a_locked_facts_pin_answers_facts_locked_on_all_three():
    world = World(facts_pin=LockedPin())
    cid = await world.candidate()

    answers = [await world.get(), await world.post(f"{BASE}/{cid}/confirm"), await world.post(f"{BASE}/{cid}/reject")]

    assert [r.status_code for r in answers] == [403, 403, 403]
    assert [await r.get_json() for r in answers] == [{"error": "facts_locked"}] * 3
    assert world.doc(cid)["status"] == "candidate"


async def test_a_failed_final_enqueue_still_answers_200():
    world = World(queue=FailingQueue())
    cid = await world.candidate()

    resp = await world.post(f"{BASE}/{cid}/confirm")

    assert resp.status_code == 200 and world.doc(cid)["status"] == "confirmed"


async def test_without_the_inbox_the_three_routes_answer_501():
    world = World()
    plain = Quart("no_inbox")
    verifier, headers = cabinet_session_for("usr-1", "acc-1")
    plain.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), cabinet_sessions=verifier, user_repo=MagicMock(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), connectors=ConnectorRegistry()))
    client = plain.test_client()

    answers = [await client.get(BASE, headers=headers), await client.post(f"{BASE}/c1/confirm", json={}, headers=headers),
               await client.post(f"{BASE}/c1/reject", json={}, headers=headers)]

    assert [r.status_code for r in answers] == [501, 501, 501]
    assert world.inbox.docs == {}
