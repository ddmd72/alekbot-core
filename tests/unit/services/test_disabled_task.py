"""DisabledTask: the `/worker` task of an unconfigured feature answers its refusal, never a success (S5b)."""
import logging

from src.services.disabled_task import DisabledTask


async def test_it_logs_the_reason_and_answers_the_refusal_whatever_the_payload(caplog):
    task = DisabledTask("feature_x: store not configured", 501, {"error": "store not configured"})
    with caplog.at_level(logging.WARNING):
        first = await task({"task_type": "feature_x", "user_id": "user-1"}, {"x-trace-id": "t"})
        second = await task({}, {})

    assert first == second == ({"error": "store not configured"}, 501)
    assert caplog.messages == ["[Worker] feature_x: store not configured"] * 2
    assert all(r.levelno == logging.WARNING for r in caplog.records)


async def test_a_caller_changing_one_answer_does_not_change_the_next():
    given = {"error": "store not configured"}
    task = DisabledTask("feature_x: store not configured", 501, given)
    given["error"] = "changed after construction"

    body, _ = await task({}, {})
    body["error"] = "changed by the caller"

    assert await task({}, {}) == ({"error": "store not configured"}, 501)
