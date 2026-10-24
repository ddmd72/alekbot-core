"""Facts S4 · T3a review fixes, through the real adapter on the evaluating Firestore fake.

- M1: the consolidator rewrote A to B after the owner was asked about A; a history archive on A is refused and nothing
  is written. A later version with the same text (tags only) is still archived.
- L5: when the id was followed forward, the result message keeps the history line.
- L3/L4: the stored history fact is stable and archival.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_reasons import answer_reason
from src.domain.fact_tool_result import HISTORY_FACT_ID
from tests.support.fact_kit import ACC, T0, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx

UTC = timezone.utc
ENDED = answer_reason(AnswerKind.ENDED, "q7", "digit")
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def seed_current(store):
    """Fact A, current while its question waits for the owner's answer."""
    return put_raw(
        store, "f-a", "works at Acme", state="current", is_current=True, reported_date=T0, expiry_kind="horizon",
        ttl_days=400, expires_at=T0 + timedelta(days=400), tags=["job", "consolidated"], temporal_class="dynamic",
        context_priority="high", lifecycle_events=[],
    )


async def archive_with_history(store, actor="owner"):
    return await store.adapter.update_fact(
        "f-a", {"state": "archived"}, ACC, context=ctx(actor), evidence_at=datetime.now(UTC), reason=ENDED,
        history=HISTORY,
    )


@pytest.mark.parametrize("actor", ["review", "owner"])
async def test_a_history_archive_on_a_fact_the_consolidator_rewrote_after_the_question_is_refused(store, actor):
    seed_current(store)
    rewrite = await store.adapter.update_fact(
        "f-a", {"content": "works at Beta", "expiry": EXPIRY_NONE}, ACC, context=ctx("stage1"),
    )
    assert rewrite["status"] == "updated" and rewrite["fact_id"] != "f-a", rewrite
    before = copy.deepcopy(store.docs)

    res = await archive_with_history(store, actor)

    assert res["status"] == "failed" and res["fact_id"] == "f-a", res
    assert f"f-a was rewritten since the owner was asked (now {rewrite['fact_id']})" in res["message"]
    assert HISTORY_FACT_ID not in res
    assert store.docs == before


async def test_a_later_version_with_the_same_text_is_archived_and_the_message_keeps_both_lines(store):
    seed_current(store)
    retag = await store.adapter.update_fact(
        "f-a", {"tags": ["job", "career"], "expiry": EXPIRY_NONE}, ACC, context=ctx("stage1"),
    )
    version = retag["fact_id"]
    assert retag["status"] == "updated" and version != "f-a", retag

    res = await archive_with_history(store)

    assert res["status"] == "updated" and res["fact_id"] == version and res["resolved_from"] == "f-a", res
    history_id = res[HISTORY_FACT_ID]
    assert f"history fact {history_id} created beside it" in res["message"]
    assert f"fact f-a was replaced earlier, so the update was applied to its current version {version}" in res["message"]
    assert store.docs[version]["state"] == "archived"
    assert store.docs[history_id]["tags"] == store.docs[version]["tags"]


async def test_the_stored_history_fact_is_stable_and_archival(store):
    seed_current(store)

    res = await archive_with_history(store)

    new = store.docs[res[HISTORY_FACT_ID]]
    assert (new["temporal_class"], new["context_priority"], new["context_priority_rank"]) == ("stable", "archival", 5)
