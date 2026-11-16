"""EmailAttachmentTextService, the scan path (email index v2, slice 5): a PDF the parser-only pass leaves
`scan_not_read` is read by the paid reader when a page budget allows, in a second pass. The real DocumentService over a
scripted parser and a scripted reader (both by file name), a counting mailbox, a recording sleep and a fake clock.
Asserts outcomes, reader requests and the budget's own counters. Invented cast."""
import asyncio
from typing import Dict, List, Union


from src.adapters.job_reader_page_budget import JobReaderPageBudget
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import (
    DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ParserBusy,
)
from src.domain.email_attachment import AttachmentRead
from src.ports.content_extractor_port import ContentExtractorPort
from src.services.document_service import DocumentService
from src.services.email_attachment_text_service import EmailAttachmentTextService
from tests.unit.services.test_document_service_read import FakeSplitter
from tests.unit.services.test_email_attachment_text_service import (
    CREDS, Rig, ScriptedParser, by_name, pdf, text_out,
)

LAB = "LAB-2025-03-0417.pdf"
TICKET = "ticket_tornillo_feliz.pdf"
LEASE = "lease_12_Oak_Street.pdf"
BOOKING = "RM-55120.pdf"
READER_TEXT = "Laboratory report 0417. All values within range. " * 20
REL = 100.0


def scan(pages: int) -> ExtractionOutcome:
    return ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=pages, extracted_by="parser@3")


def read_out(cost: float = 0.002) -> ExtractionOutcome:
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=READER_TEXT, pages_total=3, pages_read=3,
                             model_calls=1, model_cost=cost, extracted_by="reader@2")


class ScriptedReader(ContentExtractorPort):
    def __init__(self, script: Dict[str, Union[object, list]] = None) -> None:
        self.script = script or {}
        self.requests: List[ExtractionRequest] = []

    @property
    def extractor_id(self) -> str:
        return "reader"

    @property
    def version(self) -> int:
        return 1

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        self.requests.append(request)
        answer = self.script.get(request.name, read_out())
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, ExtractionOutcome)
        return answer


class SlowParser(ScriptedParser):
    """Yields to the loop before it answers and records the most requests in flight at once."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.inflight = self.peak = 0

    async def extract(self, request):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.01)
            return await super().extract(request)
        finally:
            self.inflight -= 1


class ScanRig(Rig):
    def __init__(self, files, *, parser, reader=None, **kw) -> None:
        super().__init__(files, parser=parser, **kw)
        self.reader = reader or ScriptedReader()
        self.document_service = DocumentService(self.parser, self.reader, FakeSplitter())
        self.service = EmailAttachmentTextService(self.mailbox, self.document_service, sleep=self._sleep,
                                                  clock=lambda: self.now)

    async def scans(self, budget, *, cache=None, deadline=None):
        return await self.service.read_attachments(
            CREDS, "m1", self.parts, account_id="acc1", deadline_at=self.now + 3600 if deadline is None else deadline,
            cache=self.cache if cache is None else cache, reader_budget=budget)


def lab_rig(pages: int = 3, **kw) -> ScanRig:
    return ScanRig({LAB: pdf("lab")}, parser=ScriptedParser({LAB: scan(pages)}), **kw)


async def test_without_a_budget_a_scan_stays_scan_not_read():
    rig = lab_rig()
    result = await rig.run()
    assert by_name(result) == {LAB: "scan_not_read"} and rig.reader.requests == []
    assert len(rig.parser.requests) == 1 and len(rig.cache) == 1


async def test_a_three_page_scan_is_read_by_the_reader_in_a_second_parser_pass():
    rig = lab_rig()
    budget = JobReaderPageBudget(10)
    (a,) = await rig.scans(budget)
    assert a.read is AttachmentRead.READ_BY_READER and a.text == READER_TEXT and a.head
    assert (a.reader_pages, a.reader_usd) == (3, 0.002)
    assert len(rig.parser.requests) == 2 and len(rig.reader.requests) == 1
    assert rig.reader.requests[0].max_model_pages == 3
    assert budget.used == 3 and len(rig.cache) == 1


async def test_a_long_scan_never_asks_the_budget_and_is_cached():
    rig = ScanRig({LEASE: pdf("lease")}, parser=ScriptedParser({LEASE: scan(11)}))
    budget = JobReaderPageBudget(100)
    result = await rig.scans(budget)
    assert by_name(result) == {LEASE: "scan_not_read"} and budget.asked == 0
    assert rig.reader.requests == [] and len(rig.cache) == 1 and len(rig.parser.requests) == 1


async def test_a_scan_with_an_unknown_page_count_never_asks_the_budget():
    rig = lab_rig(pages=0)
    budget = JobReaderPageBudget(100)
    assert by_name(await rig.scans(budget)) == {LAB: "scan_not_read"} and budget.asked == 0


async def test_a_refused_budget_is_not_cached_and_a_later_mail_with_room_reads_the_same_bytes():
    rig = lab_rig()
    refused = JobReaderPageBudget(2)
    assert by_name(await rig.scans(refused)) == {LAB: "scan_not_read"}
    assert refused.asked == 1 and refused.used == 0 and rig.cache == {} and rig.reader.requests == []
    (a,) = await rig.scans(JobReaderPageBudget(10))
    assert a.read is AttachmentRead.READ_BY_READER and len(rig.reader.requests) == 1


async def test_the_cache_serves_a_second_mail_with_the_same_bytes_at_no_cost():
    rig = lab_rig()
    budget = JobReaderPageBudget(10)
    (first,) = await rig.scans(budget)
    (second,) = await rig.scans(budget)
    assert len(rig.reader.requests) == 1 and budget.used == 3
    assert second.read is AttachmentRead.READ_BY_READER and second.text == first.text
    assert (second.reader_pages, second.reader_usd) == (0, 0.0)


async def test_under_sixty_seconds_left_nothing_is_booked_and_nothing_is_cached():
    rig = lab_rig()
    budget = JobReaderPageBudget(10)
    result = await rig.scans(budget, deadline=rig.now + 59)
    assert by_name(result) == {LAB: "scan_not_read"} and budget.asked == 0 and rig.cache == {}
    assert rig.reader.requests == []


async def test_a_reader_outage_leaves_the_scan_not_read_and_uncached():
    rig = lab_rig(reader=ScriptedReader({LAB: DriveModelUnavailable("down", model_calls=1)}))
    budget = JobReaderPageBudget(10)
    (a,) = await rig.scans(budget)
    assert a.read is AttachmentRead.SCAN_NOT_READ and a.reader_pages == 3 and rig.cache == {}
    assert budget.used == 3


async def test_a_reader_that_finds_no_text_is_skipped_no_text_and_cached():
    empty = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=3, extracted_by="reader@2")
    rig = lab_rig(reader=ScriptedReader({LAB: empty}))
    (a,) = await rig.scans(JobReaderPageBudget(10))
    assert a.outcome == "skipped:no_text" and a.reader_pages == 3 and len(rig.cache) == 1


async def test_a_busy_parser_on_the_second_pass_backs_off_then_reads():
    parser = ScriptedParser({LAB: [scan(3), ParserBusy(), scan(3)]})
    rig = ScanRig({LAB: pdf("lab")}, parser=parser)
    (a,) = await rig.scans(JobReaderPageBudget(10))
    assert rig.sleeps == [2.0] and a.read is AttachmentRead.READ_BY_READER


async def test_a_scan_is_one_of_the_three_reads_and_the_fourth_file_is_over_the_cap():
    files = {"a.pdf": pdf("a"), "b.pdf": pdf("b"), "c.pdf": pdf("c"), BOOKING: pdf("d")}
    parser = ScriptedParser({"a.pdf": scan(1), "b.pdf": scan(1), "c.pdf": scan(1), BOOKING: text_out()})
    rig = ScanRig(files, parser=parser)
    result = await rig.scans(JobReaderPageBudget(10))
    assert by_name(result) == {"a.pdf": "read_by_reader", "b.pdf": "read_by_reader", "c.pdf": "read_by_reader",
                               BOOKING: "skipped:over_cap"}


async def test_two_scans_read_at_once_against_a_five_page_budget_book_exactly_one():
    parser = SlowParser({LAB: scan(3), TICKET: scan(3)})
    rig = ScanRig({LAB: pdf("lab"), TICKET: pdf("ticket")}, parser=parser)
    budget = JobReaderPageBudget(5)
    first, second = await asyncio.gather(
        rig.service.read_attachments(CREDS, "m1", rig.parts[:1], account_id="acc1", deadline_at=rig.now + 3600,
                                     cache={}, reader_budget=budget),
        rig.service.read_attachments(CREDS, "m1", rig.parts[1:], account_id="acc1", deadline_at=rig.now + 3600,
                                     cache={}, reader_budget=budget))
    assert parser.peak == 2                     # the two mails really overlapped
    assert sorted(a.outcome for a in (*first, *second)) == ["read_by_reader", "scan_not_read"]
    assert budget.used == 3 and budget.asked == 2 and len(rig.reader.requests) == 1
