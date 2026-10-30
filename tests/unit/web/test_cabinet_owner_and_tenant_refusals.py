"""Cabinet refusals the shared `@auth_required` test cannot show (P7, inventory §4): the owner-only docs routes, and
another tenant's records reached by id.

Real session service, real repositories (email jobs, reminders, facts) on the evaluating Firestore fake; each refusal
is checked on the stored document, and each has a positive control so a route that refuses everyone cannot pass.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from quart import Quart, Response

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.agent_note import NoteCreate
from src.domain.email import IndexingJob
from src.services.cabinet_fact_service import CabinetFactService
from src.web import user_cabinet_app
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_store import FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import Tripwire, access_token, forged_token, session_service
from src.composition.connector_registry import ConnectorRegistry

OWNER, STRANGER = ("user-a", "account-a"), ("user-b", "account-b")


def _bearer(who) -> dict:
    return {"Authorization": f"Bearer {access_token(*who)}"}


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.touched: list = []
        env = SimpleNamespace(email_indexing_jobs_collection="jobs", orchestrator_notes_collection="notes",
                              domain_users_collection="users", is_production=False)
        self.jobs = FirestoreEmailJobRepository(self.db, env)
        self.notes = FirestoreAgentNoteAdapter(self.db, env)
        self.facts = FactStore()
        users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        app = Quart("cabinet_refusals")
        app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=Tripwire("invite_service", self.touched),
            session_service=session_service(),
            user_repo=users,
            fact_repo=self.facts.repo,
            embedding_service=self.facts.embedder,
            email_job_repo=self.jobs,
            agent_note_port=self.notes,
            cabinet_facts=CabinetFactService(self.facts.repo), connectors=ConnectorRegistry(),   # reviewer edit, facts S4 T2c: the route delegates to it
        ))
        self.client = app.test_client()


# --------------------------------------------------------------------------- /cabinet/docs*

_DOCS = ["/cabinet/docs", "/cabinet/docs/", "/cabinet/docs/index.html"]


@pytest.fixture
def docs_served(monkeypatch):
    """Stands in for the MkDocs build (absent in a checkout), so the owner's request is distinguishable from a 404."""
    async def serve(directory, filename):
        return Response(f"DOC:{filename}", status=200)
    monkeypatch.setattr(user_cabinet_app, "send_from_directory", serve)


class TestDocsOwnerOnly:
    @pytest.mark.parametrize("path", _DOCS)
    async def test_owner_is_served(self, monkeypatch, docs_served, path):
        monkeypatch.setattr(user_cabinet_app, "DOCS_OWNER_USER_ID", OWNER[0])
        resp = await World().client.get(path, headers=_bearer(OWNER))
        assert resp.status_code == 200 and (await resp.get_data(as_text=True)).startswith("DOC:")

    @pytest.mark.parametrize("path", _DOCS)
    @pytest.mark.parametrize("headers", [
        {},                                                   # no token
        {"Authorization": "Bearer not-a-jwt"},                # invalid
        {"Authorization": f"Bearer {forged_token(user_id=OWNER[0])}"},  # the owner's id, wrong key
        "stranger",                                           # another user's valid token
    ], ids=["no_token", "invalid", "forged_owner", "stranger"])
    async def test_everyone_else_gets_404(self, monkeypatch, docs_served, path, headers):
        monkeypatch.setattr(user_cabinet_app, "DOCS_OWNER_USER_ID", OWNER[0])
        resp = await World().client.get(path, headers=_bearer(STRANGER) if headers == "stranger" else headers)
        assert resp.status_code == 404

    @pytest.mark.parametrize("path", _DOCS)
    async def test_unset_owner_hides_the_docs_even_from_the_owner(self, monkeypatch, docs_served, path):
        monkeypatch.setattr(user_cabinet_app, "DOCS_OWNER_USER_ID", None)
        resp = await World().client.get(path, headers=_bearer(OWNER))
        assert resp.status_code == 404


# --------------------------------------------------------------------------- another tenant's gmail job

async def _stranger_job(w: World) -> None:
    now = datetime.now(timezone.utc)
    await w.jobs.create_job(IndexingJob(job_id="job-b", user_id=STRANGER[0], account_id=STRANGER[1], provider="gmail",
                                        triggered_by="cabinet", status="running", started_at=now, updated_at=now))


class TestForeignGmailJob:
    async def test_status_of_another_users_job_is_404(self):
        w = World()
        await _stranger_job(w)
        resp = await w.client.get("/api/gmail/jobs/job-b", headers=_bearer(OWNER))
        assert resp.status_code == 404
        assert "status" not in (await resp.get_json())

    async def test_cancel_of_another_users_job_is_404_and_the_job_runs_on(self):
        w = World()
        await _stranger_job(w)
        resp = await w.client.post("/api/gmail/jobs/job-b/cancel", headers=_bearer(OWNER))
        assert resp.status_code == 404
        assert w.db.collection("jobs").docs["job-b"]["status"] == "running"

    async def test_its_owner_can_cancel_it(self):
        w = World()
        await _stranger_job(w)
        resp = await w.client.post("/api/gmail/jobs/job-b/cancel", headers=_bearer(STRANGER))
        assert resp.status_code == 200
        assert w.db.collection("jobs").docs["job-b"]["status"] == "cancelled"


# --------------------------------------------------------------------------- another tenant's reminder

async def _stranger_note(w: World) -> str:
    note = await w.notes.create_note(NoteCreate(user_id=STRANGER[0], text="call mum", instruction="remind about mum",
                                                due=datetime.now(timezone.utc) + timedelta(days=1)))
    return note.note_id


class TestForeignReminder:
    async def test_update_of_another_users_reminder_is_refused_and_the_note_is_unchanged(self):
        w = World()
        note_id = await _stranger_note(w)
        before = dict(w.db.collection("notes").docs[note_id])
        resp = await w.client.put(f"/api/user/reminders/{note_id}", headers=_bearer(OWNER),
                                  json={"text": "hijacked", "instruction": "do something else"})
        assert resp.status_code == 400
        assert w.db.collection("notes").docs[note_id] == before

    async def test_delete_of_another_users_reminder_is_404_and_the_note_stays(self):
        w = World()
        note_id = await _stranger_note(w)
        resp = await w.client.delete(f"/api/user/reminders/{note_id}", headers=_bearer(OWNER))
        assert resp.status_code == 404
        assert note_id in w.db.collection("notes").docs

    async def test_its_owner_can_update_and_delete_it(self):
        w = World()
        note_id = await _stranger_note(w)
        resp = await w.client.put(f"/api/user/reminders/{note_id}", headers=_bearer(STRANGER), json={"text": "call dad"})
        assert resp.status_code == 200
        assert w.db.collection("notes").docs[note_id]["text"] == "call dad"
        resp = await w.client.delete(f"/api/user/reminders/{note_id}", headers=_bearer(STRANGER))
        assert resp.status_code == 200
        assert note_id not in w.db.collection("notes").docs


# --------------------------------------------------------------------------- another account's fact

async def _stranger_fact(w: World) -> str:
    res = await w.facts.adapter.create_fact("mother is Maria", {
        "account_id": STRANGER[1], "user_id": STRANGER[0], "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {}, "type": "event", "expiry": EXPIRY_NONE},
        context=ctx())
    assert res["status"] == "created", res
    return res["fact_id"]


class TestForeignFact:
    async def test_invalidating_another_accounts_fact_is_403_and_the_fact_stays_valid(self):
        w = World()
        fact_id = await _stranger_fact(w)
        resp = await w.client.post(f"/api/user/facts/{fact_id}/invalidate", headers=_bearer(OWNER))
        assert resp.status_code == 403
        doc = w.facts.docs[fact_id]
        assert doc["state"] == "current" and doc["valid_to"] is None

    async def test_its_account_can_invalidate_it(self):
        w = World()
        fact_id = await _stranger_fact(w)
        resp = await w.client.post(f"/api/user/facts/{fact_id}/invalidate", headers=_bearer(STRANGER))
        assert resp.status_code == 200
        doc = w.facts.docs[fact_id]
        assert doc["state"] == "invalidated" and doc["valid_to"] is not None
