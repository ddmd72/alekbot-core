"""The worker REST endpoint `/external-tasks/v1/*` (EXTERNAL_TASK_QUEUE_RFC §5.5, §5.6, §5.7, §6.3, §7).

The world: the real `ExternalTaskService` over the in-memory stores of `tests/support/external_task_stores.py` (the
same world as `tests/unit/services/test_external_task_service.py`), the real `FirestoreUserRepository` on `EvalDb`, the
real `FirestoreWindowCounter` on a second `EvalDb`, and the blueprint on a Quart app. Assertions are on HTTP answers,
what the stores hold and the reads the key store served.

Alex Doe is `usr-1` (account `acc-1`) with the workers `research` and `calendar-watch`; `usr-2` is another user.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_window_counter import FirestoreWindowCounter
from src.domain.external_task import (
    MAX_RENEWALS_PER_TASK,
    RATE_WINDOW_S,
    RESULT_MAX_BYTES,
    ExternalTaskStatus,
    TaskOrigin,
)
from src.domain.external_worker_key import WORKER_KEY_PREFIX
from src.domain.secret_digest import fingerprint
from src.domain.user import InactiveReason, UserProfile
from src.services import external_task_rate_limiter as limiter_module
from src.services.external_task_rate_limiter import ExternalTaskRateLimiter
from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from src.web import external_task_api as api
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.external_task_stores import FakeExternalTaskStore, FakeExternalWorkerStore
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import FakeWhitelistRepo
from tests.support.recording_task_queue import make_task_queue

T0 = UTC_MIDNIGHT.utc
DIGEST_KEY = bytes(range(32))
USERS = "users"
RATES = "eval_worker_task_dedup"
ORIGIN = TaskOrigin(platform="slack", channel_id="C-garden", session_id="usr-1:C-garden")
TITLE = "Irrigation kits"
TEXT = "compare three community-garden irrigation kits for Shelbyville"
BASE = "/external-tasks/v1"
UNKNOWN_KEY = WORKER_KEY_PREFIX + "A" * 43
ADDRESS = {"X-Forwarded-For": "203.0.113.9"}


class RecordingLogger:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def _add(self, message: str, *a: Any, **k: Any) -> None:
        self.lines.append(message)

    info = warning = error = debug = _add


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.rates_db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, is_production=False)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        self.tasks = FakeExternalTaskStore()
        self.workers = FakeExternalWorkerStore()
        self.queue, _ = make_task_queue()
        self.now = T0
        self._ids = 0
        self._bytes = 0
        self.service = ExternalTaskService(self.tasks, self.workers, self.queue, self.users,
                                           InactiveUserCheck(self.users), digest_key=DIGEST_KEY,
                                           clock=lambda: self.now, new_id=self._new_id,
                                           random_bytes=self._random_bytes)
        self.whitelist = FakeWhitelistRepo(emails=("alex@example.com", "other@example.com"))
        self.limiter = ExternalTaskRateLimiter(
            FirestoreWindowCounter(self.rates_db, SimpleNamespace(worker_task_dedup_collection=RATES)),
            clock=lambda: self.now)
        app = Quart("external_task_api_test")
        app.register_blueprint(api.create_external_task_api_blueprint(
            self.service, limiter=self.limiter, user_repo=self.users, whitelist_repo=self.whitelist))
        self.client = app.test_client()

    def _new_id(self) -> str:
        self._ids += 1
        return f"id-{self._ids}"

    def _random_bytes(self, n: int) -> bytes:
        self._bytes += 1
        return bytes([self._bytes % 256]) * n

    def at(self, seconds: float) -> None:
        self.now = T0 + timedelta(seconds=seconds)

    async def seed(self) -> "World":
        for uid, acc, email in (("usr-1", "acc-1", "alex@example.com"), ("usr-2", "acc-2", "other@example.com")):
            await self.users.create_user(UserProfile(user_id=uid, account_id=acc, email=email, display_name=uid))
            await self.db.collection(USERS).document(uid).update({"config.external_tasks_enabled": True})
        self.research = await self.service.create_worker("usr-1", "research", "web research")
        self.watch = await self.service.create_worker("usr-1", "calendar-watch")
        self.theirs = await self.service.create_worker("usr-2", "research")
        self.key = (await self.service.mint_key("usr-1", self.research.worker_id)).key
        self.watch_key = (await self.service.mint_key("usr-1", self.watch.worker_id)).key
        self.their_key = (await self.service.mint_key("usr-2", self.theirs.worker_id)).key
        return self

    async def create(self, user_id: str = "usr-1", label: str = "research", tainted: bool = False):
        return await self.service.create(user_id, label, TITLE, TEXT, ORIGIN, tainted=tainted)

    async def call(self, method: str, path: str, key: Optional[str] = None, *, headers: Optional[dict] = None,
                   **kwargs: Any):
        sent = {**ADDRESS, **(headers or {})}
        if key is not None:
            sent["Authorization"] = f"Bearer {key}"
        return await self.client.open(BASE + path, method=method, headers=sent, **kwargs)

    async def claim(self, key: Optional[str] = None) -> Dict[str, Any]:
        response = await self.call("POST", "/claim", key or self.key)
        assert response.status_code == 200
        return await response.get_json()

    async def claimed(self, key: Optional[str] = None) -> Dict[str, Any]:
        await self.create()
        return await self.claim(key)

    def rate_docs(self) -> int:
        return len(self.rates_db.collection(RATES).docs)


@pytest.fixture
async def world() -> World:
    return await World().seed()


@pytest.fixture
def logs(monkeypatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(api, "logger", recorder)
    return recorder


async def body(response) -> Dict[str, Any]:
    return json.loads(await response.get_data())


# -------------------------------------------------------------------------------------------------- the routes

async def test_a_worker_lists_its_queued_and_leased_tasks_and_nothing_else(world):
    queued = await world.create()
    leased = await world.create()
    held = await world.create(tainted=True)
    await world.create(label="calendar-watch")
    await world.create(user_id="usr-2")
    await world.claim()          # the oldest of this worker's tasks is leased
    response = await world.call("GET", "/tasks", world.key)
    assert response.status_code == 200
    listed = (await body(response))["tasks"]
    assert {t["task_id"]: t["status"] for t in listed} == {queued.task_id: "leased", leased.task_id: "queued"}
    assert held.task_id not in {t["task_id"] for t in listed}
    assert "lease_id" not in json.dumps(listed) and TEXT not in json.dumps(listed)


async def test_claim_is_204_when_there_is_no_task(world):
    response = await world.call("POST", "/claim", world.key)
    assert response.status_code == 204 and await response.get_data() == b""


async def test_a_held_task_is_invisible_to_a_claim(world):
    await world.create(tainted=True)
    assert (await world.call("POST", "/claim", world.key)).status_code == 204


async def test_a_claim_answers_provenance_fields_first_then_title_and_text(world):
    created = await world.create()
    response = await world.call("POST", "/claim", world.key)
    assert response.status_code == 200
    claim = await body(response)
    assert list(claim) == ["code", "task_id", "created_at", "lease_id", "lease_until", "origin", "title", "text"]
    assert claim["task_id"] == created.task_id and claim["code"] == "ok"
    assert claim["origin"] == "written by the user's assistant at the user's request"
    assert (claim["title"], claim["text"]) == (TITLE, TEXT)
    assert datetime.fromisoformat(claim["created_at"]) == T0


async def test_a_lease_is_renewed_with_its_id(world):
    claim = await world.claimed()
    world.at(60)
    response = await world.call("POST", f"/tasks/{claim['task_id']}/renew", world.key,
                                json={"lease_id": claim["lease_id"]})
    assert response.status_code == 200
    renewed = await body(response)
    assert renewed["code"] == "ok" and renewed["task_id"] == claim["task_id"]
    assert datetime.fromisoformat(renewed["lease_until"]) > datetime.fromisoformat(claim["lease_until"])


async def test_a_stale_lease_is_409(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/renew", world.key, json={"lease_id": "stale"})
    assert (response.status_code, await body(response)) == (409, {"code": "stale_lease"})


async def test_the_renewal_cap_is_409(world):
    claim = await world.claimed()
    for _ in range(MAX_RENEWALS_PER_TASK):
        assert (await world.call("POST", f"/tasks/{claim['task_id']}/renew", world.key,
                                 json={"lease_id": claim["lease_id"]})).status_code == 200
    response = await world.call("POST", f"/tasks/{claim['task_id']}/renew", world.key,
                                json={"lease_id": claim["lease_id"]})
    assert (response.status_code, await body(response)) == (409, {"code": "renew_cap"})


async def test_a_result_is_submitted_and_stored(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key,
                                json={"lease_id": claim["lease_id"], "result": "Kit B drips best."})
    assert response.status_code == 200
    receipt = await body(response)
    assert receipt["code"] == "ok" and receipt["task_id"] == claim["task_id"] and receipt["bytes"] == 17
    assert world.tasks.tasks[claim["task_id"]].status is ExternalTaskStatus.DONE
    assert world.tasks.results[claim["task_id"]][0] == "Kit B drips best."


async def test_the_same_submit_twice_is_the_same_answer(world):
    claim = await world.claimed()
    sent = {"lease_id": claim["lease_id"], "result": "Kit B drips best."}
    first = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key, json=sent)
    again = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key, json=sent)
    assert (again.status_code, await again.get_data()) == (first.status_code, await first.get_data())
    assert again.status_code == 200


async def test_another_body_after_done_is_409(world):
    claim = await world.claimed()
    path = f"/tasks/{claim['task_id']}/result"
    await world.call("POST", path, world.key, json={"lease_id": claim["lease_id"], "result": "Kit B drips best."})
    response = await world.call("POST", path, world.key, json={"lease_id": claim["lease_id"], "result": "Kit C."})
    assert (response.status_code, await body(response)) == (409, {"code": "already_submitted"})
    assert world.tasks.results[claim["task_id"]][0] == "Kit B drips best."


async def test_a_submit_on_a_stale_lease_is_409(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key,
                                json={"lease_id": "stale", "result": "late"})
    assert (response.status_code, await body(response)) == (409, {"code": "stale_lease"})
    assert world.tasks.tasks[claim["task_id"]].status is ExternalTaskStatus.LEASED


async def test_a_released_task_can_be_claimed_again(world):
    claim = await world.claimed()
    released = await world.call("POST", f"/tasks/{claim['task_id']}/release", world.key,
                                json={"lease_id": claim["lease_id"]})
    assert (released.status_code, await body(released)) == (200, {"code": "ok"})
    assert world.tasks.tasks[claim["task_id"]].status is ExternalTaskStatus.QUEUED
    assert (await world.claim())["task_id"] == claim["task_id"]


async def test_a_failed_task_is_final_with_the_workers_reason(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/fail", world.key,
                                json={"lease_id": claim["lease_id"], "reason": "the site is down"})
    assert (response.status_code, await body(response)) == (200, {"code": "ok"})
    assert world.tasks.tasks[claim["task_id"]].status is ExternalTaskStatus.FAILED
    view = await world.service.get_task("usr-1", claim["task_id"])
    assert view.failure_reason == "the site is down"


async def test_a_fail_without_a_reason_is_accepted(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/fail", world.key,
                                json={"lease_id": claim["lease_id"]})
    assert response.status_code == 200


# ------------------------------------------------------------------------------------------------- sizes and types

async def test_a_raw_body_over_the_cap_is_413_before_it_is_parsed(world):
    claim = await world.claimed()
    huge = b"{" + b"x" * (api.BODY_CAP_BYTES + 1)         # not JSON: a parse would be a 400
    response = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key, data=huge,
                                headers={"Content-Type": "application/json"})
    assert (response.status_code, await body(response)) == (413, {"code": "too_large"})
    assert world.tasks.tasks[claim["task_id"]].status is ExternalTaskStatus.LEASED


async def test_a_result_over_the_result_cap_is_413_from_the_service_and_the_lease_stays(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key,
                                json={"lease_id": claim["lease_id"], "result": "x" * (RESULT_MAX_BYTES + 1)})
    assert (response.status_code, await body(response)) == (413, {"code": "too_large"})
    retry = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key,
                             json={"lease_id": claim["lease_id"], "result": "short"})
    assert retry.status_code == 200


async def test_a_post_with_a_body_must_be_json(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key, data="lease_id=x&result=y",
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert (response.status_code, await body(response)) == (415, {"code": "unsupported_media_type"})
    plain = await world.call("POST", "/claim", world.key, data=b"hello", headers={"Content-Type": "text/plain"})
    assert plain.status_code == 415


@pytest.mark.parametrize("sent", [b"not json", b"[1, 2]", b'"text"', b'{"lease_id": 7, "result": "x"}',
                                  b'{"result": "x"}', b'{"lease_id": "l"}', b'{"lease_id": "l", "result": 5}',
                                  b"\xff\xfe"])
async def test_a_body_that_is_not_the_expected_object_is_400(world, sent):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key, data=sent,
                                headers={"Content-Type": "application/json"})
    assert (response.status_code, await body(response)) == (400, {"code": "bad_request"})


async def test_a_blank_result_is_400_from_the_service_and_the_lease_stays(world):
    claim = await world.claimed()
    response = await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key,
                                json={"lease_id": claim["lease_id"], "result": "   "})
    assert (response.status_code, await body(response)) == (400, {"code": "invalid_text"})
    assert world.tasks.tasks[claim["task_id"]].status is ExternalTaskStatus.LEASED


# ----------------------------------------------------------------------------------------------------- tenancy

async def test_another_workers_another_users_and_a_missing_task_are_one_identical_404(world):
    theirs = await world.service.create("usr-2", "research", TITLE, TEXT, ORIGIN, tainted=False)
    watch_task = await world.create(label="calendar-watch")
    mine = await world.claimed()
    answers = []
    for task_id in (theirs.task_id, watch_task.task_id, "no-such-task"):
        for path, payload in (("renew", {"lease_id": mine["lease_id"]}),
                              ("result", {"lease_id": mine["lease_id"], "result": "x"}),
                              ("release", {"lease_id": mine["lease_id"]}),
                              ("fail", {"lease_id": mine["lease_id"], "reason": "r"})):
            response = await world.call("POST", f"/tasks/{task_id}/{path}", world.key, json=payload)
            answers.append((response.status_code, await response.get_data()))
    assert set(answers) == {(404, json.dumps({"code": "not_found"}).encode())}
    assert world.tasks.tasks[theirs.task_id].status is ExternalTaskStatus.QUEUED
    assert world.tasks.tasks[watch_task.task_id].status is ExternalTaskStatus.QUEUED


async def test_a_held_task_is_a_404_to_every_worker_call(world):
    held = await world.create(tainted=True)
    mine = await world.claimed()
    response = await world.call("POST", f"/tasks/{held.task_id}/renew", world.key, json={"lease_id": mine["lease_id"]})
    assert (response.status_code, await body(response)) == (404, {"code": "not_found"})


async def test_a_workers_listing_and_claim_never_show_other_workers_or_users(world):
    await world.create(label="calendar-watch")
    await world.create(user_id="usr-2")
    assert (await body(await world.call("GET", "/tasks", world.key)))["tasks"] == []
    assert (await world.call("POST", "/claim", world.key)).status_code == 204
    other = await world.claim(world.their_key)
    assert other["task_id"] in world.tasks.tasks and world.tasks.tasks[other["task_id"]].user_id == "usr-2"


# ------------------------------------------------------------------------------------------------------ 401

async def test_a_missing_malformed_unknown_or_revoked_key_gets_one_401(world):
    minted = await world.service.mint_key("usr-1", world.research.worker_id)
    record = next(iter(k for k in world.workers.keys.values() if k.hint == minted.hint))
    await world.workers.revoke_key("usr-1", world.research.worker_id, record.key_digest, T0)
    cases = [
        await world.call("GET", "/tasks"),
        await world.call("GET", "/tasks", headers={"Authorization": "Basic abc"}),
        await world.call("GET", "/tasks", "short"),
        await world.call("GET", "/tasks", WORKER_KEY_PREFIX + "A" * 42),
        await world.call("GET", "/tasks", UNKNOWN_KEY),
        await world.call("GET", "/tasks", minted.key),
    ]
    assert {(r.status_code, await r.get_data()) for r in cases} == {(401, json.dumps({"code": "key_invalid"}).encode())}


async def test_a_malformed_key_costs_no_store_read_and_a_well_formed_one_costs_one(world):
    before = world.workers.get_key_reads
    await world.call("GET", "/tasks")
    await world.call("GET", "/tasks", "alekwk_short")
    await world.call("GET", "/tasks", "x" * 50)
    assert world.workers.get_key_reads == before
    await world.call("GET", "/tasks", UNKNOWN_KEY)
    assert world.workers.get_key_reads == before + 1


async def test_a_401_flood_turns_into_429_with_retry_after_and_no_more_key_reads(world, monkeypatch):
    monkeypatch.setattr(limiter_module, "UNAUTHORIZED_PER_ADDRESS_PER_WINDOW", 3)
    statuses = [(await world.call("GET", "/tasks", UNKNOWN_KEY)).status_code for _ in range(3)]
    assert statuses == [401, 401, 401]
    over = await world.call("GET", "/tasks", UNKNOWN_KEY)
    assert (over.status_code, await body(over)) == (429, {"code": "rate_limited"})
    assert 1 <= int(over.headers["Retry-After"]) <= RATE_WINDOW_S
    reads, writes = world.workers.get_key_reads, world.rate_docs()
    for key in (UNKNOWN_KEY, world.key, None):          # even the owner's real key from that address is refused
        refused = await world.call("GET", "/tasks", key)
        assert refused.status_code == 429 and "Retry-After" in refused.headers
    assert world.workers.get_key_reads == reads
    assert world.rate_docs() == writes == 0


async def test_a_401_flood_and_keyless_calls_write_no_document(world, monkeypatch):
    monkeypatch.setattr(limiter_module, "UNAUTHORIZED_PER_ADDRESS_PER_WINDOW", 2)
    for key in (None, "short", UNKNOWN_KEY, UNKNOWN_KEY, UNKNOWN_KEY, None):
        await world.call("GET", "/tasks", key)
    assert world.rate_docs() == 0


async def test_the_tracked_401_windows_are_bounded(world, monkeypatch):
    monkeypatch.setattr(limiter_module, "MAX_TRACKED_UNAUTHORIZED", 5)
    for n in range(20):
        await world.call("GET", "/tasks", UNKNOWN_KEY, headers={"X-Forwarded-For": f"198.51.100.{n}"})
    assert len(world.limiter._unauthorized) <= 5


async def test_another_address_is_not_limited_by_this_ones_flood(world, monkeypatch):
    monkeypatch.setattr(limiter_module, "UNAUTHORIZED_PER_ADDRESS_PER_WINDOW", 2)
    for _ in range(3):
        await world.call("GET", "/tasks", UNKNOWN_KEY)
    assert (await world.call("GET", "/tasks", UNKNOWN_KEY)).status_code == 429
    elsewhere = await world.call("GET", "/tasks", UNKNOWN_KEY, headers={"X-Forwarded-For": "198.51.100.7"})
    assert elsewhere.status_code == 401
    assert (await world.call("GET", "/tasks", world.key, headers={"X-Forwarded-For": "198.51.100.7"})).status_code == 200


async def test_a_forged_left_forwarded_for_entry_does_not_dodge_the_address_limit(world, monkeypatch):
    monkeypatch.setattr(limiter_module, "UNAUTHORIZED_PER_ADDRESS_PER_WINDOW", 2)
    for n in range(3):
        await world.call("GET", "/tasks", UNKNOWN_KEY, headers={"X-Forwarded-For": f"10.0.0.{n}, 203.0.113.9"})
    refused = await world.call("GET", "/tasks", UNKNOWN_KEY, headers={"X-Forwarded-For": "10.0.0.99, 203.0.113.9"})
    assert refused.status_code == 429


async def test_the_global_401_limit_holds_across_addresses(world, monkeypatch):
    monkeypatch.setattr(limiter_module, "UNAUTHORIZED_GLOBAL_PER_WINDOW", 3)
    for n in range(3):
        assert (await world.call("GET", "/tasks", UNKNOWN_KEY,
                                 headers={"X-Forwarded-For": f"198.51.100.{n}"})).status_code == 401
    over = await world.call("GET", "/tasks", UNKNOWN_KEY, headers={"X-Forwarded-For": "198.51.100.200"})
    assert over.status_code == 429 and "Retry-After" in over.headers
    reads = world.workers.get_key_reads
    assert (await world.call("GET", "/tasks", UNKNOWN_KEY, headers={"X-Forwarded-For": "192.0.2.1"})).status_code == 429
    assert world.workers.get_key_reads == reads


async def test_the_limit_clears_when_the_window_ends(world, monkeypatch):
    monkeypatch.setattr(limiter_module, "UNAUTHORIZED_PER_ADDRESS_PER_WINDOW", 2)
    for _ in range(3):
        await world.call("GET", "/tasks", UNKNOWN_KEY)
    assert (await world.call("GET", "/tasks", world.key)).status_code == 429
    world.at(RATE_WINDOW_S + 1)
    assert (await world.call("GET", "/tasks", world.key)).status_code == 200


# ------------------------------------------------------------------------------------------------------ 403

async def test_a_user_not_on_the_whitelist_gets_403_not_enabled(world):
    world.whitelist.entry.allowed_emails.discard("alex@example.com")
    response = await world.call("GET", "/tasks", world.key)
    assert (response.status_code, await body(response)) == (403, {"code": "not_enabled"})
    assert (await world.call("GET", "/tasks", world.their_key)).status_code == 200


async def test_a_user_with_no_email_gets_403_not_enabled(world):
    await world.db.collection(USERS).document("usr-1").update({"email": None})
    response = await world.call("GET", "/tasks", world.key)
    assert (response.status_code, await body(response)) == (403, {"code": "not_enabled"})


async def test_the_flag_off_is_403_not_enabled(world):
    await world.db.collection(USERS).document("usr-1").update({"config.external_tasks_enabled": False})
    response = await world.call("POST", "/claim", world.key)
    assert (response.status_code, await body(response)) == (403, {"code": "not_enabled"})


async def test_a_locked_account_is_403_account_locked(world):
    await world.db.collection(USERS).document("usr-1").update(
        {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})
    response = await world.call("POST", "/claim", world.key)
    assert (response.status_code, await body(response)) == (403, {"code": "account_locked"})


async def test_a_revoked_worker_is_403(world):
    await world.workers.revoke_worker("usr-1", world.research.worker_id, T0)
    response = await world.call("GET", "/tasks", world.key)
    assert (response.status_code, await body(response)) == (403, {"code": "worker_revoked"})


# ------------------------------------------------------------------------------------------------------ 429

async def test_a_worker_over_its_call_limit_gets_429_and_another_worker_does_not(monkeypatch):
    monkeypatch.setattr(api, "CALLS_PER_WINDOW", 2)       # read when the blueprint is built, so before the world
    world = await World().seed()
    statuses = [(await world.call("GET", "/tasks", world.key)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    over = await world.call("GET", "/tasks", world.key)
    assert over.status_code == 429 and 1 <= int(over.headers["Retry-After"]) <= RATE_WINDOW_S
    assert (await world.call("GET", "/tasks", world.watch_key)).status_code == 200
    assert (await world.call("GET", "/tasks", world.their_key)).status_code == 200
    world.at(RATE_WINDOW_S + 1)
    assert (await world.call("GET", "/tasks", world.key)).status_code == 200


async def test_claims_and_submits_have_their_own_limits(monkeypatch):
    monkeypatch.setattr(api, "CLAIMS_PER_WINDOW", 2)
    monkeypatch.setattr(api, "SUBMITS_PER_WINDOW", 1)
    world = await World().seed()
    first = await world.claimed()
    assert (await world.call("POST", "/claim", world.key)).status_code == 204
    assert (await world.call("POST", "/claim", world.key)).status_code == 429
    assert (await world.call("GET", "/tasks", world.key)).status_code == 200      # claims do not use up the listing
    sent = {"lease_id": first["lease_id"], "result": "done"}
    assert (await world.call("POST", f"/tasks/{first['task_id']}/result", world.key, json=sent)).status_code == 200
    again = await world.call("POST", f"/tasks/{first['task_id']}/result", world.key, json=sent)
    assert again.status_code == 429 and "Retry-After" in again.headers


# ------------------------------------------------------------------------------------------------------- logs

async def test_no_key_text_result_or_header_is_logged(world, logs):
    claim = await world.claimed()
    await world.call("POST", f"/tasks/{claim['task_id']}/result", world.key,
                     json={"lease_id": claim["lease_id"], "result": "Kit B drips best."})
    await world.call("GET", "/tasks", UNKNOWN_KEY)
    await world.call("GET", "/tasks", headers={"X-Forwarded-For": "198.51.100.7"})
    text = "\n".join(logs.lines)
    for secret in (world.key, UNKNOWN_KEY, TEXT, "Kit B drips best.", claim["lease_id"], "203.0.113.9", "198.51.100.7"):
        assert secret not in text
    assert f"worker={fingerprint(world.research.worker_id)}" in text and "user=usr-1" in text
    assert "[TaskAPI]" in text
