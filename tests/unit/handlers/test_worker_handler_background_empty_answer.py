"""A background turn that ends with no answer is final (owner decision 2026-10-23): one ERROR line, one ops alert,
the work item is marked done, 200, no retry. Any other failure keeps the retry (5xx).

Here: the predicate that tells an empty answer from any other failure. Each runner's final path is pinned by its own
tests (tests/unit/services/test_execute_reminder_task.py, tests/unit/services/test_daily_email_review_task.py)."""
import pytest

from src.domain.agent import AgentStatus
from src.domain.notify_result import NotifyResult


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
