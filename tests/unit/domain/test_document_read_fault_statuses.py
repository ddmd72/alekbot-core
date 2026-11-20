"""SYSTEM_FAULT_STATUSES: the one set of read statuses that are the system's fault (logged at ERROR)."""
from src.domain.document_read import SYSTEM_FAULT_STATUSES, ReadStatus


def test_the_fault_statuses_are_the_systems_own():
    assert SYSTEM_FAULT_STATUSES == {
        ReadStatus.ERROR, ReadStatus.MEMORY, ReadStatus.MODEL_UNAVAILABLE, ReadStatus.PARSER_UNAVAILABLE,
    }
    assert ReadStatus.TIMEOUT not in SYSTEM_FAULT_STATUSES
    assert ReadStatus.STILL_WORKING not in SYSTEM_FAULT_STATUSES
