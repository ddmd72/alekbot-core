"""The run's tracker reads the typed write path's `FactUpdateOutcome` (facts S4 · T8e, T3c review M2): a written
outcome is a write, a content change records the version written, a history fact records itself; a refused outcome
records nothing. The outcome is returned as the port gave it."""
from datetime import datetime, timezone

import pytest

from src.agents.consolidation_agent import _TrackingFactUpdates
from src.domain.entities import FactState
from src.domain.fact_review_answers import ARCHIVE_ONLY
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.ports.fact_update_port import FactUpdatePort
from tests.support.fact_write import ctx

SAID = datetime(2026, 10, 24, 9, 30, tzinfo=timezone.utc)
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"


class AnsweringPort(FactUpdatePort):
    """Answers each typed update with `answer(fact_id)`."""

    def __init__(self, answer) -> None:
        self.answer = answer

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        return self.answer(fact_id)


def written(fact_id, written_id=None, history_fact_id=None):
    return FactUpdateOutcome(fact_id=fact_id, written_id=written_id or fact_id, version=2, current_id=fact_id,
                             history_fact_id=history_fact_id)


async def test_a_content_change_records_the_version_written():
    outcome = written("f-old", written_id="f-v2")
    tracker = _TrackingFactUpdates(None, AnsweringPort(lambda _: outcome))

    got = await tracker.apply_fact_update("f-old", ParsedFactUpdate(content="works at Beta"), "acc", context=ctx("review"))

    assert got is outcome
    assert tracker.wrote is True and tracker.changed == [("f-v2", "works at Beta")]


async def test_an_archive_with_history_records_the_history_fact():
    tracker = _TrackingFactUpdates(None, AnsweringPort(lambda fid: written(fid, history_fact_id="f-history")))

    await tracker.apply_fact_update("f-old", ARCHIVE_ONLY, "acc", context=ctx("owner"), evidence_at=SAID,
                                    reason="ended q=q7 (digit)", history=HISTORY)

    assert tracker.wrote is True and tracker.changed == [("f-history", HISTORY)]


@pytest.mark.parametrize("refusal", list(UpdateRefusal))
async def test_a_refused_outcome_records_nothing(refusal):
    tracker = _TrackingFactUpdates(None, AnsweringPort(lambda fid: FactUpdateOutcome.refused(fid, refusal, "no")))

    got = await tracker.apply_fact_update("f-old", ParsedFactUpdate(content="x", state=FactState.CURRENT), "acc",
                                          context=ctx("review"), history=None)

    assert got.refusal is refusal and tracker.wrote is False and tracker.changed == []
