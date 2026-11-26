"""The tasks MCP resource's tool results (EXTERNAL_TASK_QUEUE_RFC §5.4): which result text each outcome takes, in which
order, filled with which values; `is_error` and the log status. Marker texts only (`tests/support/task_mcp_texts.py`).

Alex Doe's worker `research` has the tasks t1 and t2."""
from datetime import datetime, timezone

import pytest

from src.domain import external_task_mcp_contract as c
from src.domain import external_task_mcp_result as r
from src.domain.external_task import FILE_LINK_TTL_S, RESULT_MAX_BYTES, ExternalTaskStatus, TaskFile
from src.domain.external_task_refusal import TaskRefusal
from src.domain.external_task_views import ClaimedTask, LeaseGrant, SubmitReceipt, TaskFileLink, WorkerTaskSummary
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


# ------------------------------------------------------------------------------------------- task files (§5.12)

PLAN_FILE = TaskFile(number=1, name="Tom's plan.docx", mime="application/vnd.ms-word", size=2048,
                     key="external_tasks/usr-1/t1/1-Tom's plan.docx")
CERTIFICATE_FILE = TaskFile(number=2, name="birth-certificate.pdf", mime="application/pdf", size=4096,
                            key="external_tasks/usr-1/t1/2-birth-certificate.pdf")


def test_a_claim_of_a_task_with_files_lists_them_after_the_title_and_before_the_brief_with_no_link_or_key():
    task = ClaimedTask(task_id="t1", created_at=CREATED, lease_id="L1", lease_until=UNTIL, title="Plan review",
                       text=BRIEF, files=(PLAN_FILE, CERTIFICATE_FILE))
    answer = r.claimed(TEXTS, task)
    assert answer.text.split("\n") == [
        f"<CLAIMED {CREATED.isoformat()} L1 {UNTIL.isoformat()} t1>", "<WRITTEN_BY>", "<TITLE Plan review>",
        "<FILES_HEAD 2>",
        "<FILE_LINE application/vnd.ms-word Tom's plan.docx 1 2048>",
        "<FILE_LINE application/pdf birth-certificate.pdf 2 4096>",
        "<TASK_TEXT>", "compare three community-garden irrigation kits for Shelbyville",
        "line two {not a placeholder}"]
    assert "external_tasks/" not in answer.text and (answer.is_error, answer.status) == (False, "ok")


def test_a_file_link_answer_carries_the_link_its_lifetime_and_the_file():
    link = TaskFileLink(task_id="t1", number=2, name="birth-certificate.pdf", mime="application/pdf", size=4096,
                        url="https://storage.example.test/signed/x?n=1", ttl_seconds=FILE_LINK_TTL_S)
    answer = r.file_link(TEXTS, link)
    assert answer.text == (f"<FILE_LINK application/pdf birth-certificate.pdf 2 {FILE_LINK_TTL_S} 4096 t1 "
                           "https://storage.example.test/signed/x?n=1>")
    assert (answer.is_error, answer.status) == (False, "ok")


@pytest.mark.parametrize("reason", [TaskRefusal.NO_SUCH_FILE, TaskRefusal.FILE_GONE, TaskRefusal.LINK_UNAVAILABLE])
def test_each_file_refusal_has_its_own_line_with_its_code(reason):
    answer = r.refused(TEXTS, reason)
    assert answer == r.TaskToolAnswer(f"<{c.text_key_of_refusal(reason)} {reason.value}>", True, reason.value)
