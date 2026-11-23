"""The Cabinet's External workers routes (EXTERNAL_TASK_QUEUE_RFC §5.6, §5.10) over the real `ExternalTaskService` on the
in-memory stores of `tests/support/external_task_stores.py`, the real `FirestoreUserRepository` on `EvalDb` (flag and
lock written into the stored user) and a signed-in session of `tests/support/cabinet_session_kit.py`.

Alex Doe is `usr-1` (account `acc-1`); `usr-2` is another user with the flag. No answer carries a key digest, and a key
is in the answer of the mint only.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import logging
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.external_task import MAX_KEYS_PER_WORKER, MAX_WORKERS_PER_USER, TaskOrigin
from src.domain.external_task_refusal import ExternalTaskRefused
from src.domain.external_worker_key import has_key_shape
from src.domain.user import InactiveReason, UserProfile
from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_session_kit import CabinetSessionKit
from tests.support.external_task_stores import FakeExternalTaskStore, FakeExternalWorkerStore
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue
USERS = "users"
ORIGIN = TaskOrigin(platform="slack", channel_id="C-garden", session_id="usr-1:C-garden")
BODY = {"label": "research", "description": "web research for home projects"}


class Site:
    def __init__(self, with_service: bool = True) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, is_production=False)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        self.tasks = FakeExternalTaskStore()
        self.workers = FakeExternalWorkerStore()
        self.queue, self.client = make_task_queue()
        self._n = 0
        self.service = ExternalTaskService(self.tasks, self.workers, self.queue, self.users,
                                           InactiveUserCheck(self.users), digest_key=bytes(range(32)),
                                           clock=lambda: datetime.now(timezone.utc),
                                           new_id=self._new_id, random_bytes=self._random_bytes)
        self.kit = CabinetSessionKit()
        self.app = Quart("workers")
        self.app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), cabinet_sessions=self.kit.verifier, user_repo=MagicMock(),
            fact_repo=MagicMock(), embedding_service=MagicMock(),
            external_tasks=self.service if with_service else None))
        self.http = self.app.test_client()
        self.bodies = []

    def _new_id(self) -> str:
        self._n += 1
        return f"wrk-{self._n}"

    def _random_bytes(self, n: int) -> bytes:
        self._n += 1
        return bytes([self._n % 256]) * n

    async def user(self, uid: str, acc: str, flag: bool = True) -> dict:
        await self.users.create_user(UserProfile(user_id=uid, account_id=acc, email=f"{uid}@example.com",
                                                 display_name=uid))
        await self.set_flag(uid, flag)
        return (await self.kit.sign_in(uid, acc)).bearer

    async def set_flag(self, uid: str, on: bool) -> None:
        await self.db.collection(USERS).document(uid).update({"config.external_tasks_enabled": on})

    async def lock(self, uid: str) -> None:
        await self.db.collection(USERS).document(uid).update(
            {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})

    async def call(self, auth: dict, method: str, path: str, body=None):
        response = await self.http.open(path, method=method, headers=auth, json=body)
        text = await response.get_data(as_text=True)
        self.bodies.append(text)
        return response.status_code, (await response.get_json(silent=True))


@pytest.fixture
async def site():
    s = Site()
    s.alex = await s.user("usr-1", "acc-1")
    s.other = await s.user("usr-2", "acc-2")
    return s


async def _add(site: Site, auth=None, body=None) -> dict:
    status, data = await site.call(auth or site.alex, "POST", "/api/external-workers", body or BODY)
    assert status == 200, data
    return data


async def test_a_new_worker_is_listed_with_its_label_and_description_and_no_keys(site):
    added = await _add(site)
    status, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert status == 200 and data["enabled"] is True
    assert [(w["worker_id"], w["label"], w["description"], w["keys"]) for w in data["workers"]] == \
        [(added["worker_id"], "research", "web research for home projects", [])]


async def test_a_user_without_the_flag_gets_enabled_false_and_every_mutation_is_refused(site):
    await site.set_flag("usr-1", False)
    assert await site.call(site.alex, "GET", "/api/external-workers") == (200, {"enabled": False})
    for method, path, body in (("POST", "/api/external-workers", BODY), ("DELETE", "/api/external-workers/wrk-1", None),
                               ("POST", "/api/external-workers/wrk-1/keys", None),
                               ("DELETE", "/api/external-workers/wrk-1/keys/abcd1234", None)):
        assert await site.call(site.alex, method, path, body) == (404, {"error": "not_found"}), path
    assert site.workers.workers == {} and site.workers.keys == {}


async def test_a_server_without_the_service_answers_as_a_user_without_the_flag():
    s = Site(with_service=False)
    auth = await s.user("usr-1", "acc-1")
    assert await s.call(auth, "GET", "/api/external-workers") == (200, {"enabled": False})
    assert await s.call(auth, "POST", "/api/external-workers", BODY) == (404, {"error": "not_found"})


async def test_a_mint_answer_carries_the_key_and_no_later_read_does(site):
    worker = await _add(site)
    status, minted = await site.call(site.alex, "POST", f"/api/external-workers/{worker['worker_id']}/keys")
    assert status == 200 and has_key_shape(minted["key"]) and minted["hint"] and minted["key_id"]
    site.bodies.clear()
    status, data = await site.call(site.alex, "GET", "/api/external-workers")
    [key] = data["workers"][0]["keys"]
    assert key["key_id"] == minted["key_id"] and key["hint"] == minted["hint"] and key["last_used_at"] is None
    assert set(key) == {"key_id", "hint", "created_at", "last_used_at"}
    assert minted["key"] not in "".join(site.bodies)
    stored = next(iter(site.workers.keys.values()))
    assert stored.key_digest not in "".join(site.bodies) and stored.key_digest != minted["key_id"]


async def test_a_revoked_key_is_gone_from_the_list_and_the_service_refuses_it(site):
    worker = await _add(site)
    _, minted = await site.call(site.alex, "POST", f"/api/external-workers/{worker['worker_id']}/keys")
    await site.service.resolve_key(minted["key"])
    status, _ = await site.call(site.alex, "DELETE",
                                f"/api/external-workers/{worker['worker_id']}/keys/{minted['key_id']}")
    assert status == 200
    with pytest.raises(ExternalTaskRefused):
        await site.service.resolve_key(minted["key"])
    _, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert data["workers"][0]["keys"] == []
    again = await site.call(site.alex, "DELETE", f"/api/external-workers/{worker['worker_id']}/keys/{minted['key_id']}")
    assert again == (404, {"error": "not_found"})


async def test_an_unknown_key_id_is_not_found(site):
    worker = await _add(site)
    assert await site.call(site.alex, "DELETE", f"/api/external-workers/{worker['worker_id']}/keys/00000000") == \
        (404, {"error": "not_found"})


async def test_removing_a_worker_cancels_its_queued_task_and_its_keys(site):
    worker = await _add(site)
    _, minted = await site.call(site.alex, "POST", f"/api/external-workers/{worker['worker_id']}/keys")
    created = await site.service.create("usr-1", "research", "Irrigation kits", "compare three kits", ORIGIN,
                                        tainted=False)
    status, data = await site.call(site.alex, "DELETE", f"/api/external-workers/{worker['worker_id']}")
    assert (status, data) == (200, {"cancelled": 1})
    assert (await site.service.get_task("usr-1", created.task_id)).summary.status.value == "cancelled"
    with pytest.raises(ExternalTaskRefused):
        await site.service.resolve_key(minted["key"])
    _, listed = await site.call(site.alex, "GET", "/api/external-workers")
    assert listed["workers"] == []


async def test_a_locked_user_gets_423_on_every_route(site):
    worker = await _add(site)
    await site.lock("usr-1")
    for method, path, body in (("GET", "/api/external-workers", None), ("POST", "/api/external-workers", BODY),
                               ("DELETE", f"/api/external-workers/{worker['worker_id']}", None),
                               ("POST", f"/api/external-workers/{worker['worker_id']}/keys", None),
                               ("DELETE", f"/api/external-workers/{worker['worker_id']}/keys/abcd1234", None)):
        assert await site.call(site.alex, method, path, body) == (423, {"error": "account_locked"}), path


async def test_another_users_worker_is_not_found_for_every_operation(site):
    theirs = await _add(site, site.other)
    wid = theirs["worker_id"]
    _, minted = await site.call(site.other, "POST", f"/api/external-workers/{wid}/keys")
    for method, path in (("DELETE", f"/api/external-workers/{wid}"), ("POST", f"/api/external-workers/{wid}/keys"),
                         ("DELETE", f"/api/external-workers/{wid}/keys/{minted['key_id']}")):
        assert await site.call(site.alex, method, path) == (404, {"error": "not_found"}), path
    _, mine = await site.call(site.alex, "GET", "/api/external-workers")
    assert mine["workers"] == []
    _, theirs_now = await site.call(site.other, "GET", "/api/external-workers")
    assert len(theirs_now["workers"]) == 1 and len(theirs_now["workers"][0]["keys"]) == 1


@pytest.mark.parametrize(("body", "error"), [({"label": "Not Valid!"}, "label_invalid"), ({"label": ""}, "label_invalid")])
async def test_an_invalid_label_is_400(site, body, error):
    assert await site.call(site.alex, "POST", "/api/external-workers", body) == (400, {"error": error})


async def test_a_taken_label_and_the_caps_are_400_with_their_code(site):
    first = await _add(site)
    assert await site.call(site.alex, "POST", "/api/external-workers", BODY) == (400, {"error": "label_taken"})
    for _ in range(MAX_KEYS_PER_WORKER):
        await site.call(site.alex, "POST", f"/api/external-workers/{first['worker_id']}/keys")
    assert await site.call(site.alex, "POST", f"/api/external-workers/{first['worker_id']}/keys") == \
        (400, {"error": "keys_cap"})
    for i in range(MAX_WORKERS_PER_USER - 1):
        await _add(site, body={"label": f"w{i}"})
    assert await site.call(site.alex, "POST", "/api/external-workers", {"label": "one-too-many"}) == \
        (400, {"error": "workers_cap"})


async def test_no_route_answers_401_to_a_signed_in_user_and_a_malformed_body_is_400(site):
    assert await site.call(site.alex, "POST", "/api/external-workers", {"label": 5}) == (400, {"error": "invalid_body"})
    assert await site.call(site.alex, "POST", "/api/external-workers", {"label": "ok", "description": 5}) == \
        (400, {"error": "invalid_body"})


async def test_no_log_line_carries_a_key_or_a_description(site, caplog):
    caplog.set_level(logging.DEBUG)
    worker = await _add(site)
    _, minted = await site.call(site.alex, "POST", f"/api/external-workers/{worker['worker_id']}/keys")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert minted["key"] not in text and BODY["description"] not in text
