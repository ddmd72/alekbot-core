"""S3-D12: on a cleanup pass (stage2a, stage2b, cluster, backstop) a past `date` refusal has no DISCARD; it points to
history (`{kind: none}`) or a future date. Stage 1 and email keep the text that offers DISCARD of the candidate."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.exceptions import FactBornDueError
from src.domain.fact_expiry import NOTHING_WRITTEN, parse_expiry
from src.domain.fact_lifecycle import born_due, compute_expiry
from src.domain.pass_actor import PassActor
from tests.support.fact_write import on

LISBON = "Europe/Lisbon"
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
REPORTED = NOW - timedelta(days=30)


def refusal(date_text, *, cleanup):
    fields = compute_expiry(parse_expiry(on(date_text)), REPORTED, LISBON)
    with pytest.raises(FactBornDueError) as refused:
        born_due(fields, REPORTED, NOW, LISBON, PassActor.STAGE2A if cleanup else PassActor.STAGE1)
    return str(refused.value)


def test_the_cleanup_refusal_offers_history_or_a_future_date_and_never_discard():
    text = refusal("2026-10-20", cleanup=True)
    assert "the date 2026-10-20 is past (today is 2026-10-23 in Europe/Lisbon)" in text
    assert "discard" not in text.lower()
    assert "do not drop the fact" in text
    assert "{kind: none}" in text and "past tense" in text
    assert "give a future date if the plan still holds" in text
    assert text.endswith(NOTHING_WRITTEN)


def test_the_conversation_refusal_text_is_unchanged():
    text = refusal("2026-10-20", cleanup=False)
    assert "DISCARD the candidate if it no longer matters" in text
    assert "{kind: none}" in text and "do not drop" not in text


@pytest.mark.parametrize("cleanup", [False, True])
def test_a_date_of_today_is_the_same_short_horizon_request_on_both_kinds_of_pass(cleanup):
    text = refusal("2026-10-23", cleanup=cleanup)
    assert "a plan for today is not over yet" in text and "discard" not in text.lower()
