"""The parser slot when the deadline has already passed and the slot is taken: no wait, ParserBusy, a WARNING."""
import logging
import time

import pytest

from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_extraction import ParserBusy


async def test_a_taken_slot_and_a_passed_deadline_is_parser_busy_at_once(tmp_path, caplog):
    parser = SubprocessContentExtractor(max_children=1, temp_root=str(tmp_path))
    await parser._slots.acquire()
    caplog.set_level(logging.WARNING)
    with pytest.raises(ParserBusy):
        await parser._acquire_slot({"op": "extract"}, time.time() - 5)
    assert any(r.levelno == logging.WARNING and "slot stayed busy" in r.getMessage() and "extract" in r.getMessage()
               for r in caplog.records)
    parser._slots.release()
    await parser._acquire_slot({"op": "extract"}, time.time() - 5)    # free again: taken at once despite the passed deadline
    assert parser._slots.locked()
