"""The consolidation run's tracker forwards the review keywords of `update_fact` (facts S4 · T3a): `evidence_at`, `reason`
and `history` reach the port unchanged, and the write is still recorded. A consolidation call sets none of them and
reaches the port with the same keywords as before."""
from datetime import datetime, timezone
from typing import Any, Dict, List

from src.agents.consolidation_agent import _TrackingFactManagement
from src.domain.fact_tool_result import HISTORY_FACT_ID
from src.ports.fact_management_port import FactManagementPort
from tests.support.fact_write import ctx

SAID = datetime(2026, 10, 24, 9, 30, tzinfo=timezone.utc)
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"


class RecordingPort(FactManagementPort):
    """A port that archives in memory and answers like the adapter: an update with history creates one more fact."""

    def __init__(self) -> None:
        self.updates: List[Dict[str, Any]] = []
        self.facts: Dict[str, str] = {"f-old": "current"}

    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20, *, timezone):
        return []

    async def create_fact(self, content, metadata, *, context):
        return {"fact_id": None, "status": "failed", "message": "unused"}

    async def update_fact(self, fact_id, updates, account_id, *, context, **review):
        self.updates.append({"fact_id": fact_id, "updates": updates, "context": context, **review})
        self.facts[fact_id] = updates.get("state", self.facts[fact_id])
        result = {"fact_id": fact_id, "status": "updated", "version": 1, "message": "ok"}
        if review.get("history"):
            self.facts["f-history"] = "current"
            result[HISTORY_FACT_ID] = "f-history"
        return result

    async def merge_facts(self, fact_ids, merged_content, metadata, *, context):
        return {"new_fact_id": None, "old_fact_ids": [], "status": "failed", "message": "unused"}


async def test_the_tracker_forwards_all_three_keywords_and_records_the_write():
    port = RecordingPort()
    tracker = _TrackingFactManagement(port)
    owner = ctx("owner")

    result = await tracker.update_fact(
        "f-old", {"state": "archived"}, "acc", context=owner, evidence_at=SAID, reason="ended q=q7 (digit)",
        history=HISTORY,
    )

    assert port.updates == [{
        "fact_id": "f-old", "updates": {"state": "archived"}, "context": owner,
        "evidence_at": SAID, "reason": "ended q=q7 (digit)", "history": HISTORY,
    }]
    assert result[HISTORY_FACT_ID] == "f-history" and port.facts == {"f-old": "archived", "f-history": "current"}
    assert tracker.wrote is True
    assert tracker.changed == [("f-history", HISTORY)]


async def test_a_revive_forwards_its_evidence_date_and_reason_without_history():
    port = RecordingPort()
    tracker = _TrackingFactManagement(port)

    await tracker.update_fact(
        "f-old", {"state": "current"}, "acc", context=ctx("review"), evidence_at=SAID, reason="revived on e1234567",
    )

    (call,) = port.updates
    assert (call["evidence_at"], call["reason"]) == (SAID, "revived on e1234567") and "history" not in call
    assert tracker.wrote is True and tracker.changed == []


async def test_a_consolidation_update_reaches_the_port_with_no_review_keywords():
    port = RecordingPort()
    tracker = _TrackingFactManagement(port)

    await tracker.update_fact("f-old", {"content": "works at Beta"}, "acc", context=ctx("stage1"))

    (call,) = port.updates
    assert set(call) == {"fact_id", "updates", "context"}
    assert tracker.changed == [("f-old", "works at Beta")]
