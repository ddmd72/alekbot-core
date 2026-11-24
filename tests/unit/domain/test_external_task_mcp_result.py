"""The tasks MCP resource's tool results (EXTERNAL_TASK_QUEUE_RFC §5.4): which result text each outcome takes, in which
order, filled with which values; `is_error` and the log status. Marker texts only (`tests/support/task_mcp_texts.py`).

Alex Doe's worker `research` has the tasks t1 and t2."""
from datetime import datetime, timezone

import pytest

from src.domain import external_task_mcp_contract as c
from src.domain import external_task_mcp_result as r
from src.domain.external_task import RESULT_MAX_BYTES, ExternalTaskStatus
from src.domain.external_task_refusal import TaskRefusal
from src.domain.external_task_views import ClaimedTask, LeaseGrant, SubmitReceipt, WorkerTaskSummary
from src.domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR
from tests.support.task_mcp_texts import marker_results

TEXTS = marker_results()
CREATED = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 11, 24, 9, 15, tzinfo=timezone.utc)
BRIEF = "compare three community-garden irrigation kits for Shelbyville\nline two {not a placeholder}"


def test_a_claim_puts_the_code_written_provenance_first_then_the_title_then_the_brief_plain():
    task = ClaimedTask(task_id="t1", created_at=CREATED, lease_id="L1", lease_until=UNTIL, title="Irrigation kits",
                       text=BRIEF)
    answer = r.claimed(TEXTS, task)
    assert answer.text.split("\n") == [
        f"<CLAIMED {CREATED.isoformat()} L1 {UNTIL.isoformat()} t1>", "<WRITTEN_BY>", "<TITLE Irrigation kits>",
        "<TASK_TEXT>", "compare three community-garden irrigation kits for Shelbyville",
        "line two {not a placeholder}"]
    assert (answer.is_error, answer.status) == (False, "ok")


def test_a_claim_that_found_no_task_is_a_success():
    answer = r.claimed(TEXTS, None)
    assert (answer.text, answer.is_error, answer.status) == ("<NO_TASK>", False, "no_task")


def test_a_listing_names_each_task_by_its_state_in_the_services_order():
    tasks = [WorkerTaskSummary(task_id="t1", title="Irrigation kits", status=ExternalTaskStatus.LEASED,
                               created_at=CREATED, lease_until=UNTIL),
             WorkerTaskSummary(task_id="t2", title="Term calendar", status=ExternalTaskStatus.QUEUED,
                               created_at=CREATED)]
    answer = r.listed(TEXTS, tasks)
    assert answer.text.split("\n") == [
        "<LIST_HEAD 2>",
        f"<LIST_LEASED {CREATED.isoformat()} {UNTIL.isoformat()} t1 Irrigation kits>",
        f"<LIST_QUEUED {CREATED.isoformat()} t2 Term calendar>"]
    assert (answer.is_error, answer.status) == (False, "ok")


def test_an_empty_listing_is_its_own_line():
    assert r.listed(TEXTS, []).text == "<LIST_EMPTY>"


def test_the_lease_and_result_answers():
    assert r.renewed(TEXTS, LeaseGrant(task_id="t1", lease_id="L1", lease_until=UNTIL)).text == \
        f"<RENEWED {UNTIL.isoformat()} t1>"
    receipt = SubmitReceipt(task_id="t1", bytes=42, sha256="ab" * 32)
    replay = SubmitReceipt(task_id="t1", bytes=42, sha256="ab" * 32, replay=True)
    assert r.submitted(TEXTS, receipt) == r.submitted(TEXTS, replay)
    assert r.submitted(TEXTS, receipt).text == f"<SUBMITTED 42 {'ab' * 32} t1>"
    assert r.released(TEXTS, "t1").text == "<RELEASED t1>"
    assert r.failed(TEXTS, "t1").text == "<FAILED t1>"
    assert not any(a.is_error for a in (r.renewed(TEXTS, LeaseGrant("t1", "L1", UNTIL)), r.submitted(TEXTS, receipt),
                                        r.released(TEXTS, "t1"), r.failed(TEXTS, "t1")))


def test_an_empty_id_and_a_rate_limit_are_errors_with_their_lines():
    assert r.id_required(TEXTS, "lease_id") == r.TaskToolAnswer("<ID_REQUIRED lease_id>", True, "id_required")
    assert r.rate_limited(TEXTS, 30) == r.TaskToolAnswer("<RATE_LIMITED 30>", True, "rate_limited")


@pytest.mark.parametrize("code", c.WORKER_REFUSALS)
def test_every_worker_refusal_has_its_own_line_with_its_code(code):
    answer = r.refused(TEXTS, code)
    expected = (f"<E_TOO_LARGE {code.value} {RESULT_MAX_BYTES}>" if code is TaskRefusal.TOO_LARGE
                else f"<{c.text_key_of_refusal(code)} {code.value}>")
    assert (answer.text, answer.is_error, answer.status) == (expected, True, code.value)


@pytest.mark.parametrize("code", [TaskRefusal.NOT_ENABLED, TaskRefusal.WORKER_REVOKED, TaskRefusal.KEY_INVALID,
                                  TaskRefusal.UNKNOWN_WORKER])
def test_access_refusals_and_any_other_code_share_the_neutral_line(code):
    answer = r.refused(TEXTS, code)
    assert (answer.text, answer.is_error, answer.status) == ("<REFUSED>", True, code.value)


def test_a_lock_between_verify_and_call_answers_the_lock_text():
    answer = r.refused(TEXTS, TaskRefusal.ACCOUNT_LOCKED)
    assert (answer.text, answer.is_error, answer.status) == (ACCOUNT_LOCKED_TOOL_ERROR, True, "account_locked")
