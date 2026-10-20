"""Both Smart-bound worker paths tell notify() who started the run (AGENT_WORKSPACE_RFC §9); a reminder fire carries
its fire key, the same on every Cloud Tasks re-run of that fire (§10 row 1)."""
from unittest.mock import MagicMock

from src.domain.agent import AgentStatus
from src.domain.notify_result import NotifyResult
from tests.unit.handlers.test_worker_handler import (
    _ACC_A,
    _EXECUTE_NOTE_ID,
    _EXECUTE_PAYLOAD,
    _USER_A,
    _make_full_worker,
    _make_reminder_note,
)

FIRE = f"reminder:{_EXECUTE_NOTE_ID}@2026-05-01T06:05:00+00:00"


def _reminder_worker(delivered=True):
    worker, ns = _make_full_worker()
    ns.notes_port.get_note.return_value = _make_reminder_note()
    profile = MagicMock()
    profile.account_id = "acc-x"
    ns.user_repo.get_user.return_value = profile
    ns.notification.notify.return_value = NotifyResult(
        delivered=delivered, agent_status=AgentStatus.SUCCESS if delivered else AgentStatus.FAILED,
    )
    return worker, ns


async def test_execute_reminder_passes_origin_and_fire_key():
    worker, ns = _reminder_worker()
    _, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
    assert status == 200
    kw = ns.notification.notify.call_args.kwargs
    assert kw["origin_detail"] == "execute_reminder"
    assert kw["fire_key"] == FIRE


async def test_a_rerun_of_the_same_fire_has_the_same_key():
    worker, ns = _reminder_worker(delivered=False)
    _, first = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
    _, second = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
    assert (first, second) == (500, 500)
    keys = [c.kwargs["fire_key"] for c in ns.notification.notify.call_args_list]
    assert keys == [FIRE, FIRE]


async def test_daily_review_passes_its_origin():
    worker, ns = _make_full_worker()
    ns.email_review.fetch_review_payload.return_value = [{"email_id": "e1", "subject": "Hello"}]
    ns.notification.notify.return_value = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)
    _, status = await worker._handle_daily_email_review({"user_id": _USER_A, "account_id": _ACC_A})
    assert status == 200
    kw = ns.notification.notify.call_args.kwargs
    assert kw["origin_detail"] == "daily_email_review"
    assert "fire_key" not in kw
