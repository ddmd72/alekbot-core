"""Mutation P3 (consolidation_agent): `_TrackingFactUpdates.apply_fact_update` forwards `expected_id` to the typed port."""
from datetime import datetime, timezone

from src.agents.consolidation_agent import _TrackingFactUpdates
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_outcome import FactUpdateOutcome
from src.domain.fact_write_context import FactWriteContext
from src.ports.fact_update_port import FactUpdatePort


class _RecordingTyped(FactUpdatePort):
    def __init__(self) -> None:
        self.calls: list = []

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        self.calls.append(dict(fact_id=fact_id, expected_id=expected_id, evidence_at=evidence_at, reason=reason,
                               history=history))
        return FactUpdateOutcome(fact_id=fact_id, written_id="fact-2", version=2, current_id=fact_id)


async def test_tracker_forwards_expected_id_to_typed_port():
    typed = _RecordingTyped()
    tracker = _TrackingFactUpdates(real=None, typed=typed)  # the typed path never touches `real`
    ctx = FactWriteContext(actor="review", timezone="UTC", span=None)
    at = datetime(2026, 11, 3, tzinfo=timezone.utc)
    await tracker.apply_fact_update("fact-1", ParsedFactUpdate(content="x"), "acc-1", context=ctx, evidence_at=at,
                                    reason="r", history="h", expected_id="fact-0")
    assert typed.calls == [dict(fact_id="fact-1", expected_id="fact-0", evidence_at=at, reason="r", history="h")]
