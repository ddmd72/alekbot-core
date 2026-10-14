"""The reader and triage report their calls and cost to the tick summary (USER_DRIVE_INDEXING_RFC §24.9)."""
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageBatchOutcome, TriageResult
from src.domain.drive_index import TriageVerdict


def test_an_outcome_without_model_work_reports_none():
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="x")
    assert (outcome.model_calls, outcome.model_cost) == (0, 0.0)


def test_the_reader_reports_calls_and_cost():
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="x", pages_sent=12, model_calls=2,
                                model_cost=0.0071)
    assert (outcome.model_calls, outcome.model_cost) == (2, 0.0071)


def test_a_triage_batch_carries_its_results_calls_and_cost():
    batch = TriageBatchOutcome(results=(TriageResult("a", TriageVerdict.PHOTO, "a dog"),), model_calls=3,
                               model_cost=0.0004)
    assert (len(batch.results), batch.model_calls, batch.model_cost) == (1, 3, 0.0004)
    assert (TriageBatchOutcome(results=()).model_calls, TriageBatchOutcome(results=()).model_cost) == (0, 0.0)
