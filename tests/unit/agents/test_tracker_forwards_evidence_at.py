"""The consolidation run's tracker forwards the review keywords (facts S4 · T3a): `evidence_at`, `reason` and `history`
reach the port unchanged, and the write is still recorded. A consolidation call sets none of them and reaches the port
with the same keywords as before.

Reviewer edit (facts S4, T3c): the review keywords left the model's `update_fact`; they travel with code's typed
`apply_fact_update` (FactUpdatePort), which the tracker forwards whole. The recording port implements both entries.
Reviewer edit (T3c fix round, L1): the typed forwarding is `_TrackingFactUpdates`, built by the run's `_tracker_for`.
Reviewer edit (S4 T6b, T3c re-review L6): `_tracker_for` is gone; the run's tracker is `_TrackingFactUpdates(model port,
typed port)`, and production passes the one port as both, as here."""
from datetime import datetime, timezone
from typing import Any, Dict, List

from src.agents.consolidation_agent import _TrackingFactManagement, _TrackingFactUpdates
from src.domain.entities import FactState
from src.domain.fact_review_answers import ARCHIVE_ONLY
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_outcome import FactUpdateOutcome
from src.ports.fact_management_port import FactManagementPort
from src.ports.fact_update_port import FactUpdatePort
from tests.support.fact_write import ctx

SAID = datetime(2026, 10, 24, 9, 30, tzinfo=timezone.utc)
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"
REVIVE = ParsedFactUpdate(state=FactState.CURRENT)


class RecordingPort(FactManagementPort, FactUpdatePort):
    """A port that archives in memory and answers like the adapter: an update with history creates one more fact."""

    def __init__(self) -> None:
        self.updates: List[Dict[str, Any]] = []
        self.facts: Dict[str, str] = {"f-old": "current"}

    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20, *, timezone):
        return []

    async def create_fact(self, content, metadata, *, context):
        return {"fact_id": None, "status": "failed", "message": "unused"}

    async def update_fact(self, fact_id, updates, account_id, *, context):
        self.updates.append({"fact_id": fact_id, "updates": updates, "context": context})
        self.facts[fact_id] = updates.get("state", self.facts[fact_id])
        return {"fact_id": fact_id, "status": "updated", "version": 1, "message": "ok"}

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        self.updates.append({"fact_id": fact_id, "update": update, "context": context,
                             "evidence_at": evidence_at, "reason": reason, "history": history})
        if update.state is not None:
            self.facts[fact_id] = update.state.value
        # Reviewer edit (S4 T8e): the typed path answers with a typed outcome (T3c review M2), not the model's dict.
        history_id = None
        if history:
            self.facts["f-history"] = "current"
            history_id = "f-history"
        return FactUpdateOutcome(fact_id=fact_id, written_id=fact_id, version=1, current_id=fact_id,
                                 history_fact_id=history_id)

    async def merge_facts(self, fact_ids, merged_content, metadata, *, context):
        return {"new_fact_id": None, "old_fact_ids": [], "status": "failed", "message": "unused"}


async def test_the_tracker_forwards_all_three_keywords_and_records_the_write():
    port = RecordingPort()
    tracker = _TrackingFactUpdates(port, port)
    assert isinstance(tracker, _TrackingFactUpdates)
    owner = ctx("owner")

    result = await tracker.apply_fact_update(
        "f-old", ARCHIVE_ONLY, "acc", context=owner, evidence_at=SAID, reason="ended q=q7 (digit)", history=HISTORY,
    )

    assert port.updates == [{
        "fact_id": "f-old", "update": ARCHIVE_ONLY, "context": owner,
        "evidence_at": SAID, "reason": "ended q=q7 (digit)", "history": HISTORY,
    }]
    assert result.history_fact_id == "f-history" and port.facts == {"f-old": "archived", "f-history": "current"}
    assert tracker.wrote is True
    assert tracker.changed == [("f-history", HISTORY)]


async def test_a_revive_forwards_its_evidence_date_and_reason_without_history():
    port = RecordingPort()
    tracker = _TrackingFactUpdates(port, port)

    await tracker.apply_fact_update("f-old", REVIVE, "acc", context=ctx("review"), evidence_at=SAID,
                                    reason="revived on e1234567")

    (call,) = port.updates
    # The typed port names all three keywords; no history reaches it.
    assert (call["evidence_at"], call["reason"], call["history"]) == (SAID, "revived on e1234567", None)
    assert call["update"] == REVIVE
    assert tracker.wrote is True and tracker.changed == []


async def test_a_consolidation_update_reaches_the_port_with_no_review_keywords():
    port = RecordingPort()
    tracker = _TrackingFactManagement(port)

    await tracker.update_fact("f-old", {"content": "works at Beta"}, "acc", context=ctx("stage1"))

    (call,) = port.updates
    assert set(call) == {"fact_id", "updates", "context"}
    assert tracker.changed == [("f-old", "works at Beta")]
