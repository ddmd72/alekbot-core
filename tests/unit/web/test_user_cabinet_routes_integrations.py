"""Cabinet integration write routes not covered elsewhere (P19): `DELETE /api/gmail/data` over the real indexed-email
repository, and the Microsoft To Do routes, whose collaborator (`task_setup`, `tasks_provider`) is scripted only to record
which user it was asked about and to answer.
"""
import pytest

from tests.unit.web.cabinet_routes_world import ALEX, CLARA, CabinetWorld, MAILS


class _TaskSetup:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list = []
        self.fail = fail

    async def get_status(self, user_id):
        return {"connected": True, "user": user_id}

    async def reindex_all(self, user_id):
        self.calls.append(("reindex", user_id))
        if self.fail:
            raise RuntimeError("boom")

    async def disconnect(self, user_id):
        self.calls.append(("disconnect", user_id))
        if self.fail:
            raise RuntimeError("boom")


class _Lists:
    async def list_task_lists(self, user_id):
        from src.domain.task import TaskList
        return [TaskList(list_id=f"list-of-{user_id}", name="Groceries")]


async def _seed_mails(w: CabinetWorld) -> None:
    for doc_id, user_id in (("usr-1_m1", "usr-1"), ("usr-1_m2", "usr-1"), ("usr-2_m1", "usr-2")):
        await w.db.collection(MAILS).document(doc_id).set({"user_id": user_id})


class TestDeleteGmailData:
    async def test_only_the_callers_indexed_mails_are_deleted(self):
        w = CabinetWorld()
        await _seed_mails(w)
        resp = await w.client.delete("/api/gmail/data", headers=w.token(ALEX))
        assert resp.status_code == 200
        assert set(w.db.collection(MAILS).docs) == {"usr-2_m1"}

    async def test_the_other_user_deletes_only_their_own(self):
        w = CabinetWorld()
        await _seed_mails(w)
        await w.client.delete("/api/gmail/data", headers=w.token(CLARA))
        assert set(w.db.collection(MAILS).docs) == {"usr-1_m1", "usr-1_m2"}

    async def test_without_a_valid_token_nothing_is_deleted(self):
        w = CabinetWorld()
        await _seed_mails(w)
        resp = await w.client.delete("/api/gmail/data", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401
        assert len(w.db.collection(MAILS).docs) == 3

    async def test_without_the_repository_it_is_501(self):
        from quart import Quart
        from src.web.user_cabinet_app import create_user_cabinet_blueprint
        w = CabinetWorld()
        app = Quart("no_mail_repo")
        app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=w.invite_service, session_service=w.sessions, user_repo=w.users,
            fact_repo=w.facts.repo, embedding_service=w.facts.embedder))
        resp = await app.test_client().delete("/api/gmail/data", headers=w.token(ALEX))
        assert resp.status_code == 501


class TestMicrosoftTasks:
    async def test_every_call_is_made_for_the_token_user(self):
        setup = _TaskSetup()
        w = CabinetWorld(task_setup=setup, tasks_provider=_Lists())
        assert (await w.client.post("/api/tasks/microsoft/reindex", headers=w.token(ALEX))).status_code == 200
        assert (await w.client.delete("/api/tasks/microsoft/disconnect", headers=w.token(CLARA))).status_code == 200
        assert setup.calls == [("reindex", "usr-1"), ("disconnect", "usr-2")]
        lists = await (await w.client.get("/api/tasks/microsoft/lists", headers=w.token(ALEX))).get_json()
        assert lists == {"lists": [{"id": "list-of-usr-1", "name": "Groceries"}]}
        status = await (await w.client.get("/api/tasks/microsoft/status", headers=w.token(ALEX))).get_json()
        assert status["user"] == "usr-1"

    async def test_a_body_user_id_never_replaces_the_token_user(self):
        setup = _TaskSetup()
        w = CabinetWorld(task_setup=setup)
        await w.client.post("/api/tasks/microsoft/reindex", headers=w.token(ALEX), json={"user_id": "usr-2"})
        assert setup.calls == [("reindex", "usr-1")]

    @pytest.mark.parametrize("method, path", [("POST", "/api/tasks/microsoft/reindex"),
                                              ("DELETE", "/api/tasks/microsoft/disconnect")])
    async def test_a_failure_is_500_without_the_internals(self, method, path):
        w = CabinetWorld(task_setup=_TaskSetup(fail=True))
        resp = await w.client.open(path, method=method, headers=w.token(ALEX))
        assert resp.status_code == 500 and "boom" not in await resp.get_data(as_text=True)

    async def test_unwired_it_is_501_and_the_status_says_not_connected(self):
        w = CabinetWorld()
        h = w.token(ALEX)
        assert (await w.client.post("/api/tasks/microsoft/reindex", headers=h)).status_code == 501
        assert (await w.client.delete("/api/tasks/microsoft/disconnect", headers=h)).status_code == 501
        assert (await w.client.get("/api/tasks/microsoft/lists", headers=h)).status_code == 501
        assert await (await w.client.get("/api/tasks/microsoft/status", headers=h)).get_json() == {"connected": False}


class _RealShapedLists:
    """Answers with the domain `TaskList` items the real provider returns (`list_id`, no `id`)."""

    async def list_task_lists(self, user_id):
        from src.domain.task import TaskList
        return [TaskList(list_id="list-1", name="Inbox list"), TaskList(list_id="list-2", name="Backlog", is_shared=True)]


class TestMicrosoftTaskListsPayload:
    async def test_domain_task_lists_are_served_as_id_and_name(self):
        w = CabinetWorld(tasks_provider=_RealShapedLists())
        resp = await w.client.get("/api/tasks/microsoft/lists", headers=w.token(ALEX))
        assert resp.status_code == 200
        assert await resp.get_json() == {"lists": [{"id": "list-1", "name": "Inbox list"},
                                                   {"id": "list-2", "name": "Backlog"}]}
