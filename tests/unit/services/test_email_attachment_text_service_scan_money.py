"""Money rules of the scan path (email index v2, slice 5): a busy answer after a billed reader call is not retried, the
same bytes are read and paid once across mails of a page and across concurrent mails, and a failed read does not poison
the cache. Real DocumentService over scripted parser and reader; invented cast."""
import asyncio

import pytest

from scripts.email.card_script_support import MeteredAttachmentText, SpendCap, page_gate
from src.adapters.job_reader_page_budget import JobReaderPageBudget
from src.domain.drive_extraction import DriveModelUnavailable, ParserUnavailable
from src.domain.email_attachment import AttachmentRead
from tests.unit.services.test_email_attachment_text_service import CREDS, MSG, ScriptedParser, pdf
from tests.unit.services.test_email_attachment_text_service_scans import (
    LAB, TICKET, ScanRig, ScriptedReader, read_out, scan,
)


class SeqReader(ScriptedReader):
    """Answers a list in order per file name (the last repeats); an exception is raised."""

    async def extract(self, request):
        answers = self.script.get(request.name)
        if isinstance(answers, list):
            self.script = {**self.script, request.name: answers[1:] if len(answers) > 1 else answers}
            self.requests.append(request)
            answer = answers[0]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return await super().extract(request)


class GatedReader(ScriptedReader):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def extract(self, request):
        await self.release.wait()
        return await super().extract(request)


async def test_a_busy_answer_after_a_billed_reader_call_is_not_retried():
    busy = ParserUnavailable("busy", model_calls=1, model_cost=0.0009)
    rig = ScanRig({LAB: pdf("lab")}, parser=ScriptedParser({LAB: scan(3)}), reader=ScriptedReader({LAB: busy}))
    cap = SpendCap(1.0)
    metered = MeteredAttachmentText(rig.mailbox, rig.document_service, cap)
    (a,) = await metered.read_attachments(CREDS, MSG, rig.parts, account_id="acc1", deadline_at=rig.now + 3600,
                                          cache={}, reader_budget=JobReaderPageBudget(10, gate=page_gate(cap)))
    assert a.read is AttachmentRead.SCAN_NOT_READ and (a.reader_pages, a.reader_usd) == (3, 0.0009)
    assert len(rig.reader.requests) == 1 and rig.sleeps == []
    assert cap.spent == pytest.approx(0.0009) and cap.reserved == pytest.approx(0.0)


async def test_a_busy_answer_with_no_spend_still_backs_off_and_retries():
    parser = ScriptedParser({LAB: [scan(3), scan(3)]})
    busy = ParserUnavailable("busy")
    rig = ScanRig({LAB: pdf("lab")}, parser=parser, reader=SeqReader({LAB: [busy, read_out()]}))
    (a,) = await rig.scans(JobReaderPageBudget(10))
    assert rig.sleeps == [2.0] and a.read is AttachmentRead.READ_BY_READER and len(rig.reader.requests) == 2


async def test_two_mails_of_one_cache_read_and_book_the_same_scan_once():
    rig = ScanRig({LAB: pdf("lab")}, parser=ScriptedParser({LAB: scan(3)}))
    budget, cache = JobReaderPageBudget(10), {}
    first = await rig.scans(budget, cache=cache)
    second = await rig.scans(budget, cache=cache)
    assert len(rig.reader.requests) == 1 and budget.asked == 1
    assert (second[0].reader_pages, second[0].reader_usd) == (0, 0.0) and second[0].text == first[0].text


async def test_concurrent_mails_with_the_same_bytes_pay_once():
    reader = GatedReader()
    rig = ScanRig({LAB: pdf("same"), TICKET: pdf("same")}, parser=ScriptedParser({LAB: scan(3), TICKET: scan(3)}),
                  reader=reader)
    budget, cache = JobReaderPageBudget(10), {}
    tasks = [asyncio.create_task(rig.service.read_attachments(
        CREDS, MSG, [p], account_id="acc1", deadline_at=rig.now + 3600, cache=cache, reader_budget=budget))
        for p in rig.parts]
    await asyncio.sleep(0.05)
    assert not any(t.done() for t in tasks) and len(rig.service._inflight) == 1   # both mails are in, one read runs
    reader.release.set()
    one, two = await asyncio.gather(*tasks)
    assert len(reader.requests) == 1 and budget.asked == 1 and budget.used == 3
    assert sorted((one[0].reader_usd, two[0].reader_usd)) == [0.0, 0.002]
    assert one[0].read is two[0].read is AttachmentRead.READ_BY_READER and rig.service._inflight == {}


async def test_an_outage_on_the_first_leaves_the_cache_empty_and_a_later_call_reads():
    outage = DriveModelUnavailable("down", model_calls=1)
    rig = ScanRig({LAB: pdf("lab")}, parser=ScriptedParser({LAB: scan(3)}), reader=SeqReader({LAB: [outage]}))
    cache = {}
    (a,) = await rig.scans(JobReaderPageBudget(10), cache=cache)
    assert a.read is AttachmentRead.SCAN_NOT_READ and cache == {} and rig.service._inflight == {}
    rig.reader.script = {}   # the outage is over
    (b,) = await rig.scans(JobReaderPageBudget(10), cache=cache)
    assert b.read is AttachmentRead.READ_BY_READER


async def _two_calls(rig, reader, *, first_deadline, cache):
    budget = JobReaderPageBudget(10)
    tasks = [asyncio.create_task(rig.service.read_attachments(
        CREDS, MSG, [p], account_id="acc1", deadline_at=dl, cache=cache, reader_budget=budget))
        for p, dl in zip(rig.parts, (first_deadline, rig.now + 3600))]
    return budget, tasks


async def test_a_waiter_reads_for_itself_when_the_leader_had_too_little_time():
    rig = ScanRig({LAB: pdf("same"), TICKET: pdf("same")}, parser=ScriptedParser({LAB: scan(3), TICKET: scan(3)}))
    cache = {}
    budget, tasks = await _two_calls(rig, rig.reader, first_deadline=rig.now + 30, cache=cache)
    one, two = await asyncio.gather(*tasks)
    assert one[0].read is AttachmentRead.SCAN_NOT_READ          # the leader had under 60 s left
    assert two[0].read is AttachmentRead.READ_BY_READER and two[0].reader_pages == 3
    assert len(rig.reader.requests) == 1 and budget.asked == 1 and rig.service._inflight == {}


async def test_a_waiter_reads_for_itself_when_the_leader_raised():
    parser = ScriptedParser({LAB: [ParserUnavailable("unavailable"), scan(3)], TICKET: scan(3)})
    rig = ScanRig({LAB: pdf("same"), TICKET: pdf("same")}, parser=parser)
    budget, tasks = await _two_calls(rig, rig.reader, first_deadline=rig.now + 3600, cache={})
    first, second = await asyncio.gather(*tasks, return_exceptions=True)
    assert type(first).__name__ == "AttachmentParserUnavailable"
    assert second[0].read is AttachmentRead.READ_BY_READER


async def test_a_cancelled_waiter_does_not_cancel_the_leader_and_the_result_is_cached():
    reader = GatedReader()
    rig = ScanRig({LAB: pdf("same"), TICKET: pdf("same")}, parser=ScriptedParser({LAB: scan(3), TICKET: scan(3)}),
                  reader=reader)
    cache = {}
    budget = JobReaderPageBudget(10)
    leader, waiter = [asyncio.create_task(rig.service.read_attachments(
        CREDS, MSG, [p], account_id="acc1", deadline_at=rig.now + 3600, cache=cache, reader_budget=budget))
        for p in rig.parts]
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    reader.release.set()
    (a,) = await leader
    assert a.read is AttachmentRead.READ_BY_READER and len(cache) == 1 and len(reader.requests) == 1
    assert rig.service._inflight == {}
