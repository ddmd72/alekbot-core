"""A background turn that ends with no answer is final (owner decision 2026-10-23): one ERROR line, one ops alert,
the work item is marked done, 200, no retry. Any other failure keeps the retry (5xx)."""
import dataclasses
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentStatus
from src.domain.notify_result import NotifyResult
from tests.unit.handlers.test_worker_deep_research_webhook_event import _payload
from tests.unit.handlers.test_worker_deep_research_webhook_event import _worker as _dr_worker
from tests.unit.handlers.test_worker_handler import (
    _ACC_A,
    _EXECUTE_NOTE_ID,
    _EXECUTE_PAYLOAD,
    _USER_A,
    _make_full_worker,
    _make_reminder_note,
)

EMPTY = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="empty_answer")
OTHER = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="boom")
OK = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)


def _empty_records(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]


def _reminder_worker(result):
    worker, ns = _make_full_worker()
    ns.notes_port.get_note.return_value = _make_reminder_note()
    profile = MagicMock()
    profile.account_id = "acc-x"
    ns.user_repo.get_user.return_value = profile
    ns.notification.notify.return_value = result
    return worker, ns


class TestExecuteReminder:
    async def test_empty_answer_is_final_success_path(self, caplog):
        worker, ns = _reminder_worker(EMPTY)
        with caplog.at_level(logging.ERROR):
            body, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
        assert (status, body["status"]) == (200, "empty_answer_final")
        ns.notes_port.mark_fire_delivered.assert_awaited_once()
        ns.notes_port.delete_note.assert_awaited_once()  # one-time note removed
        assert len(_empty_records(caplog)) == 1
        ns.billing_webhook.post.assert_awaited_once()
        text = ns.billing_webhook.post.await_args.args[0]
        assert "execute_reminder" in text and _EXECUTE_NOTE_ID in text
        assert "ping the team" not in text  # no content

    async def test_recurring_note_is_marked_but_kept(self):
        worker, ns = _reminder_worker(EMPTY)
        ns.notes_port.get_note.return_value = dataclasses.replace(_make_reminder_note(), recurrence=MagicMock())
        _, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
        assert status == 200
        ns.notes_port.mark_fire_delivered.assert_awaited_once()
        ns.notes_port.delete_note.assert_not_awaited()

    async def test_alert_failure_does_not_break_the_final_path(self):
        worker, ns = _reminder_worker(EMPTY)
        ns.billing_webhook.post.side_effect = RuntimeError("webhook down")
        _, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
        assert status == 200
        ns.notes_port.mark_fire_delivered.assert_awaited_once()

    async def test_other_failure_still_retries(self, caplog):
        worker, ns = _reminder_worker(OTHER)
        with caplog.at_level(logging.ERROR):
            _, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
        assert status == 500
        ns.notes_port.mark_fire_delivered.assert_not_awaited()
        ns.billing_webhook.post.assert_not_awaited()
        assert _empty_records(caplog) == []

    async def test_success_unchanged(self, caplog):
        worker, ns = _reminder_worker(OK)
        with caplog.at_level(logging.ERROR):
            body, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
        assert (status, body["status"]) == (200, "ok")
        ns.notes_port.mark_fire_delivered.assert_awaited_once()
        ns.billing_webhook.post.assert_not_awaited()
        assert _empty_records(caplog) == []


class TestDailyEmailReview:
    async def _run(self, result):
        worker, ns = _make_full_worker()
        ns.email_review.fetch_review_payload.return_value = [{"email_id": "e1", "subject": "Hello"}]
        ns.notification.notify.return_value = result
        out = await worker._handle_daily_email_review({"user_id": _USER_A, "account_id": _ACC_A})
        return out, ns

    async def test_empty_answer_is_final(self, caplog):
        with caplog.at_level(logging.ERROR):
            (body, status), ns = await self._run(EMPTY)
        assert (status, body["status"]) == (200, "empty_answer_final")
        assert len(_empty_records(caplog)) == 1
        ns.billing_webhook.post.assert_awaited_once()
        assert "daily_email_review" in ns.billing_webhook.post.await_args.args[0]

    async def test_other_failure_still_retries(self):
        (_, status), ns = await self._run(OTHER)
        assert status == 500
        ns.billing_webhook.post.assert_not_awaited()

    async def test_success_unchanged(self):
        (body, status), ns = await self._run(OK)
        assert (status, body["status"]) == (200, "ok")
        ns.billing_webhook.post.assert_not_awaited()


class TestDeepResearchNotice:
    async def _run(self, result):
        worker, _, _ = _dr_worker(notify=result)
        worker._billing_webhook = AsyncMock()
        out = await worker._dispatch(_payload(event_type="response.cancelled"), {})
        return out, worker._billing_webhook

    async def test_empty_answer_is_final(self, caplog):
        with caplog.at_level(logging.ERROR):
            (body, status), alert = await self._run(EMPTY)
        assert (status, body["status"]) == (200, "empty_answer_final")
        assert len(_empty_records(caplog)) == 1
        alert.post.assert_awaited_once()

    async def test_other_failure_still_retries(self):
        (_, status), alert = await self._run(OTHER)
        assert status == 500
        alert.post.assert_not_awaited()


class TestNoAlertSinkConfigured:
    async def test_final_path_still_logs_and_returns_200(self, caplog):
        worker, ns = _reminder_worker(EMPTY)
        worker._billing_webhook = None
        with caplog.at_level(logging.ERROR):
            _, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)
        assert status == 200 and len(_empty_records(caplog)) == 1


@pytest.mark.parametrize("delivered,sent,error,expected", [
    (False, False, "empty_answer", True),
    (False, True, "empty_answer", False),
    (True, True, "empty_answer", False),
    (False, False, "other", False),
    (False, False, None, False),
])
def test_is_empty_answer_predicate(delivered, sent, error, expected):
    from src.domain.notify_result import is_empty_answer
    r = NotifyResult(delivered=delivered, agent_status=AgentStatus.FAILED, error=error, sent=sent)
    assert is_empty_answer(r) is expected
