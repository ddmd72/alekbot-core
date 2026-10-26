"""Facts S4 T8a: the `fact_review` worker task's type, payload and dedup keys."""
from datetime import datetime, timedelta, timezone

from src.domain.fact_review_task_keys import FACT_REVIEW_TASK, REVIEW_MODES, defer_key, next_key, review_payload

NOW = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)


def test_the_task_type_and_modes():
    assert FACT_REVIEW_TASK == "fact_review"
    assert REVIEW_MODES == {"daily", "next"}


def test_the_payload_names_the_user_the_account_and_the_mode():
    assert review_payload("u1", "acc", "next") == {"user_id": "u1", "account_id": "acc", "mode": "next"}


def test_losers_of_one_lease_retry_bucket_share_a_deferral_and_the_next_bucket_does_not():
    start = datetime.fromtimestamp((int(NOW.timestamp()) // 120) * 120, tz=timezone.utc)

    assert defer_key("acc", start) == defer_key("acc", start + timedelta(seconds=119))
    assert defer_key("acc", start) != defer_key("acc", start + timedelta(seconds=120))
    assert defer_key("acc", start) == f"fact-review-defer:acc:{int(start.timestamp()) // 120}"
    assert defer_key("acc", start) != defer_key("acc-2", start)


def test_a_release_is_named_after_the_closed_question():
    assert next_key("q1") == "fact-review-next:q1" != next_key("q2")
