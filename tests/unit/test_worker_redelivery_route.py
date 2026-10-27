"""The deployed `/worker` route claims a Cloud Tasks delivery by `X-CloudTasks-TaskName` and never claims a Scheduler body.

The route is main's own (`main.py`, `worker()`), booted offline by the machinery of `test_main_boots_offline.py`: the boot's
probe is replaced by a scenario that posts through the real OIDC gate with Cloud Tasks headers. The claim documents are read
from the claim store the worker handler holds, in the collection it was built with. Dropping `delivery=delivery` in the
route turns the first, second and third tests red. Gathered test: Firestore evaluation is `EvalDb`; the claim's `create`
and `get` yield there, so the two posts really interleave.
"""
import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import pytest

from tests.support import prod_env, worker_triggers
from tests.unit import test_main_boots_offline as boot_test

pytestmark = pytest.mark.slow

Answer = Tuple[int, Any]


async def _post(client: Any, body: dict, task: str = "") -> Answer:
    headers = {"Authorization": f"Bearer {prod_env.VALID_TOKEN}"}
    if task:
        headers.update({"X-CloudTasks-TaskName": task, "X-CloudTasks-TaskRetryCount": "0"})
    response = await client.post("/worker", json=body, headers=headers)
    try:
        return response.status_code, await response.get_json()
    except Exception:
        return response.status_code, None


async def _claim_doc(handler: Any, key: str) -> Any:
    store = handler._task_claims
    snap = await store.db_client.collection(store.collection_name).document(key).get()
    return snap.to_dict() if snap.exists else None


async def _run(monkeypatch, caplog, scenario: Callable[[Any, Any], Awaitable[Any]]) -> Any:
    """Boot main() once; run `scenario(client, worker_handler)` where the boot would probe."""
    outcome: Dict[str, Any] = {}

    async def probe(app, served, boot, caplog_):
        oauth_kwargs = next(kw for name, _, _, kw in boot.calls if name == "create_oauth_blueprint")
        from src.domain.billing import BillingAccount
        from src.domain.user import UserProfile
        auth = oauth_kwargs["auth_service"]
        await auth.account_repo.create_account(BillingAccount(account_id="a1"))
        await auth.user_repo.create_user(UserProfile(user_id="u1", account_id="a1"))
        handler = boot_test._worker_closure(app)["worker_handler"]
        outcome["value"] = await scenario(app.test_client(), handler)

    monkeypatch.setattr(boot_test, "_probe", probe)
    boot = await boot_test._boot(monkeypatch, caplog)
    if boot.probe_error is not None:
        raise boot.probe_error
    return outcome["value"]


def _payloads() -> Dict[str, dict]:
    return {t: {"task_type": t, **p} for t, p in boot_test.ENQUEUE_PAYLOADS.items()}


async def _two_deliveries(client: Any, handler: Any) -> Dict[str, Tuple[Answer, Answer, Any]]:
    rows = {}
    for task_type, body in sorted(_payloads().items()):
        name = f"route-{task_type}"
        first = await _post(client, body, name)
        second = await _post(client, body, name)
        rows[task_type] = (first, second, await _claim_doc(handler, name))
    return rows


async def test_a_second_delivery_of_a_claimed_task_is_a_duplicate(monkeypatch, caplog):
    rows = await _run(monkeypatch, caplog, _two_deliveries)
    checked = [t for t, (first, _, _) in rows.items() if first[0] < 500]
    assert checked, "no task type answered below 500"
    problems = []
    for task_type in checked:
        _, second, claim = rows[task_type]
        if second != (200, {"status": "duplicate"}):
            problems.append(f"{task_type}: second delivery answered {second}")
        if not claim or claim.get("state") != "done":
            problems.append(f"{task_type}: claim is {claim}")
    assert not problems, "\n".join(problems)


async def test_a_retryable_answer_releases_and_the_retry_runs(monkeypatch, caplog):
    rows = await _run(monkeypatch, caplog, _two_deliveries)
    retryable = [t for t, (first, _, _) in rows.items() if first[0] >= 500]
    assert retryable, "no task type answered 500 or above"
    problems = []
    for task_type in retryable:
        first, second, claim = rows[task_type]
        if claim is not None:
            problems.append(f"{task_type}: a claim remains after a {first[0]}: {claim}")
        if second != first:
            problems.append(f"{task_type}: retry answered {second}, first {first}")
    assert not problems, "\n".join(problems)


async def test_a_gathered_double_runs_once(monkeypatch, caplog):
    async def scenario(client: Any, handler: Any) -> List[Answer]:
        store = handler._task_claims
        store.db_client.collection(store.collection_name).yield_on_get = True
        body = _payloads()["execute_reminder"]
        return list(await asyncio.gather(_post(client, body, "route-gathered"), _post(client, body, "route-gathered")))

    answers = await _run(monkeypatch, caplog, scenario)
    ran = [a for a in answers if a != (200, {"status": "duplicate"})]
    assert len(ran) == 1, answers
    assert ran[0] == (200, {"status": "note_gone"})


async def test_scheduler_bodies_are_never_claimed(monkeypatch, caplog):
    async def scenario(client: Any, handler: Any) -> Dict[str, Tuple[Answer, Answer]]:
        rows = {}
        for task_type, body in sorted(worker_triggers.scheduler_task_types().items()):
            rows[task_type] = (await _post(client, body), await _post(client, body))
        store = handler._task_claims
        rows["__claims__"] = [d.id async for d in store.db_client.collection(store.collection_name).stream()]
        return rows

    rows = await _run(monkeypatch, caplog, scenario)
    claims = rows.pop("__claims__")
    assert rows, "no scheduler body"
    assert claims == [], claims
    assert {t: r for t, r in rows.items() if r[0] != r[1]} == {}
    assert all(r[0][0] == r[1][0] != 409 for r in rows.values())
    assert all(r[1] != (200, {"status": "duplicate"}) for r in rows.values())
