"""The parser's slot under a request deadline (EXTERNAL_FILES_RFC §4.4 "Parser slot"): with `deadline_at` the wait for
the one child slot is bounded by it and ends in ParserBusy (no child started, a WARNING logged); without one the wait is
unbounded as before; a deadline already passed starts no child; the slot is released however the call ends."""
import asyncio
import logging
import time

import pytest

from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionRequest, ParserBusy, PdfSplitFailed


@pytest.fixture
def parser(tmp_path, monkeypatch):
    """An extractor whose child is a fake that holds the slot until `gate` opens; `spawned` counts the children."""
    extractor = SubprocessContentExtractor(max_children=1, temp_root=str(tmp_path))
    extractor.gate = asyncio.Event()
    extractor.spawned = 0

    async def spawn(request, workdir, errors, wall=None):
        extractor.spawned += 1
        await extractor.gate.wait()
        return 1, False

    monkeypatch.setattr(extractor, "_spawn", spawn)
    return extractor


def _request(tmp_path, **limits):
    path = tmp_path / "lab_report_2025_03.txt"
    path.write_text("hello")
    return ExtractionRequest(path=str(path), name=path.name, kind="text", mime_type="text/plain", user_id="ann",
                             account_id="a1", **limits)


async def _holder(parser, tmp_path):
    held = asyncio.create_task(parser.extract(_request(tmp_path)))
    while parser.spawned == 0:
        await asyncio.sleep(0)
    return held


@pytest.mark.timing
async def test_a_busy_slot_past_the_deadline_raises_parser_busy_and_starts_no_child(parser, tmp_path, caplog):
    held = await _holder(parser, tmp_path)
    caplog.set_level(logging.WARNING)
    with pytest.raises(ParserBusy) as raised:
        await parser.extract(_request(tmp_path, deadline_at=time.time() + 0.2))
    assert raised.value.status is ExtractionStatus.TIMEOUT and raised.value.detail == "busy"
    assert parser.spawned == 1
    assert any(r.levelno >= logging.WARNING and "busy" in r.getMessage() for r in caplog.records)
    parser.gate.set()
    await held


@pytest.mark.timing
async def test_a_page_op_waits_for_the_slot_only_until_its_deadline(parser, tmp_path):
    held = await _holder(parser, tmp_path)
    with pytest.raises(ParserBusy):
        await parser.pdf_page_count(str(tmp_path / "scan_0012.pdf"), deadline_at=time.time() + 0.2)
    parser.gate.set()
    await held


async def test_without_a_deadline_the_wait_for_the_slot_is_unbounded(parser, tmp_path):
    held = await _holder(parser, tmp_path)
    waiting = asyncio.create_task(parser.extract(_request(tmp_path)))
    await asyncio.sleep(0.3)
    assert not waiting.done() and parser.spawned == 1
    parser.gate.set()
    await held
    await asyncio.wait_for(waiting, 5)
    assert parser.spawned == 2


async def test_a_deadline_with_time_to_spare_gets_the_slot_when_it_frees(parser, tmp_path):
    held = await _holder(parser, tmp_path)
    waiting = asyncio.create_task(parser.extract(_request(tmp_path, deadline_at=time.time() + 30)))
    await asyncio.sleep(0.1)
    parser.gate.set()
    await held
    await asyncio.wait_for(waiting, 5)
    assert parser.spawned == 2


async def test_a_deadline_already_passed_with_a_free_slot_starts_no_child(parser, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    out = await parser.extract(_request(tmp_path, deadline_at=time.time() - 1))
    assert out.status is ExtractionStatus.TIMEOUT and out.detail == "deadline" and parser.spawned == 0
    assert any(r.levelno >= logging.WARNING and "deadline" in r.getMessage() for r in caplog.records)


async def test_a_passed_deadline_on_a_page_op_is_a_split_failure_not_busy(parser, tmp_path):
    with pytest.raises(PdfSplitFailed) as raised:
        await parser.pdf_page_count(str(tmp_path / "scan_0012.pdf"), deadline_at=time.time() - 1)
    assert not isinstance(raised.value, ParserBusy) and raised.value.detail == "deadline" and parser.spawned == 0


@pytest.mark.timing
async def test_the_slot_is_released_after_a_busy_raise_and_after_a_run(parser, tmp_path):
    held = await _holder(parser, tmp_path)
    with pytest.raises(ParserBusy):
        await parser.extract(_request(tmp_path, deadline_at=time.time() + 0.1))
    parser.gate.set()
    await held
    out = await asyncio.wait_for(parser.extract(_request(tmp_path)), 5)   # the slot is free again
    assert out.status is ExtractionStatus.ERROR and parser.spawned == 2
