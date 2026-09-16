from src.domain.companion_extraction import CompanionExtractionBatch, EXTRACTION_TASK
from src.domain.consolidation import BatchStatus


def test_default_construction():
    batch = CompanionExtractionBatch(
        session_id="slack:C123",
        account_id="acc-1",
        companion_type="tutor",
        created_by_user_id="user-1",
        messages=[{"role": "user", "parts": [{"text": "hola"}]}],
    )
    assert batch.status == BatchStatus.PENDING
    assert batch.attempts == 0
    assert batch.records_extracted == 0
    assert batch.batch_id.startswith("cbatch_")


def test_extraction_task_constant_is_stable_string():
    assert EXTRACTION_TASK == "extract_companion_batch"
