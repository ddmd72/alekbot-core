"""EmailAttachmentTextService: the noise filter, the three-read cap, the cache, the busy back-off and the outcome map,
over the real DocumentService (scripted parser), an in-memory mailbox that counts downloads, a recording sleep and a
fake clock. Asserts outcomes, downloads and parser requests, not calls on mocks. Invented cast."""
import asyncio
import io
import time
import zipfile
from typing import Dict, List, Union

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ParserBusy, ParserUnavailable
from src.domain.email import EmailPart
from src.domain.email_attachment import AttachmentRead, EmailAttachment
from src.domain.email_attachment_policy import ATTACHMENT_HEAD_CHARS, ATTACHMENT_TAIL_CHARS
from src.domain.exceptions import (
    AttachmentParserUnavailable, EmailMessageGone, EmailPartTooLarge, EmailPartUnavailable,
)
from src.ports.content_extractor_port import ContentExtractorPort
from src.services.email_attachment_text_service import EmailAttachmentTextService
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.support.oauth_credentials_store import creds
from tests.unit.services.test_document_service_read import FakeExtractor, make

CREDS = creds("user-1", "gmail")
MSG = "m1"
BIG = 5000


def text_out(text: str = "Lab report 0417. All values within range. " * 30) -> ExtractionOutcome:
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=text, pages_total=2, extracted_by="parser@3")


LOCKED = ExtractionOutcome(status=ExtractionStatus.ENCRYPTED, extracted_by="parser@3")
SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=9, extracted_by="parser@3")


def pdf(tag: str) -> bytes:
    return b"%PDF-1.7\n" + tag.encode() * 20


def zipped(member: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, "<x/>")
    return buf.getvalue()


OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class CountingMailbox(FakeEmailMailbox):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.downloads: List[str] = []

    async def get_part(self, credentials, message_id, part, max_bytes):
        self.downloads.append(part.filename)
        return await super().get_part(credentials, message_id, part, max_bytes)


class ScriptedParser(ContentExtractorPort):
    """Answers by the request's file name: one outcome / exception, or a list consumed in order (the last repeats)."""

    def __init__(self, script: Dict[str, Union[object, list]], default: object = None) -> None:
        self.script, self.default = script, default or text_out()
        self.requests: List[ExtractionRequest] = []

    @property
    def extractor_id(self) -> str:
        return "parser"

    @property
    def version(self) -> int:
        return 1

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        self.requests.append(request)
        answer = self.script.get(request.name, self.default)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, ExtractionOutcome)
        return answer


class Rig:
    def __init__(self, files: Dict[str, bytes], *, parser=None, now: float = None, mime: Dict[str, str] = None,
                 sizes: Dict[str, int] = None) -> None:
        mime = mime or {}
        sizes = sizes or {}
        parts, blobs = [], {}
        for i, (name, data) in enumerate(files.items()):
            handle = f"H{i}"
            blobs[handle] = data
            parts.append(EmailPart(str(i), name, mime.get(name, ""), sizes.get(name, BIG), handle))
        self.parts = parts
        self.mailbox = CountingMailbox({MSG: parts}, blobs)
        self.parser = parser or ScriptedParser({})
        self.document_service, _, self.reader, _ = make(parser=self.parser)
        self.sleeps: List[float] = []
        self.now = time.time() if now is None else now
        self.service = EmailAttachmentTextService(self.mailbox, self.document_service, sleep=self._sleep,
                                                  clock=lambda: self.now)
        self.cache: Dict[str, EmailAttachment] = {}

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def run(self, parts=None, cache=None, deadline=None):
        return await self.service.read_attachments(CREDS, MSG, parts if parts is not None else self.parts,
                                                   account_id="acc1", deadline_at=self.now + 3600 if deadline is None else deadline,
                                                   cache=self.cache if cache is None else cache)


def by_name(result) -> Dict[str, str]:
    return {a.name: a.outcome for a in result}


async def test_noise_is_not_listed_or_downloaded_and_a_plain_image_is_listed_skipped():
    rig = Rig({"logo.png": PNG, "contact.vcf": b"x", "invite.ics": b"x", "smime.p7s": b"x", "photos.zip": b"PK",
               "voice_note.m4a": b"x", "tiny.pdf": pdf("t"), "photo.jpg": PNG},
              mime={"logo.png": "image/png", "photo.jpg": "image/jpeg"}, sizes={"tiny.pdf": 1_000})
    rig.parts[0] = EmailPart("0", "logo.png", "image/png", BIG, "H0", displayed_inline=True)
    result = await rig.run()
    assert by_name(result) == {"photo.jpg": "skipped:image"}
    assert rig.mailbox.downloads == [] and rig.parser.requests == []


async def test_five_readable_pdfs_read_three_and_skip_the_rest_undownloaded():
    rig = Rig({f"doc{i}.pdf": pdf(str(i)) for i in range(5)})
    result = await rig.run()
    assert [a.outcome for a in result] == ["text"] * 3 + ["skipped:over_cap"] * 2
    assert rig.mailbox.downloads == ["doc0.pdf", "doc1.pdf", "doc2.pdf"]
    assert len(rig.parser.requests) == 3


async def test_unreadable_downloads_do_not_take_a_read_slot():
    rig = Rig({"slides.pptx": zipped("ppt/presentation.xml"), "old_report.doc": OLE,
               "a.pdf": pdf("a"), "b.pdf": pdf("b"), "c.pdf": pdf("c")})
    result = await rig.run()
    assert by_name(result) == {"slides.pptx": "skipped:unsupported", "old_report.doc": "skipped:unsupported",
                               "a.pdf": "text", "b.pdf": "text", "c.pdf": "text"}
    assert len(rig.parser.requests) == 3


async def test_same_bytes_in_two_mails_with_one_cache_are_parsed_once_and_twice_in_a_mail_is_a_duplicate():
    rig = Rig({"LAB-2025-03-0417.pdf": pdf("x")})
    first = await rig.run()
    second = await rig.run()
    assert len(rig.parser.requests) == 1 and first == second and rig.cache
    twin = Rig({"a.pdf": pdf("x"), "b.pdf": pdf("x")})
    assert by_name(await twin.run()) == {"a.pdf": "text", "b.pdf": "skipped:duplicate"}
    assert len(twin.parser.requests) == 1


async def test_a_cache_hit_keeps_the_new_name_and_size():
    rig = Rig({"a.pdf": pdf("x")})
    await rig.run()
    other = Rig({"copy of a.pdf": pdf("x")}, sizes={"copy of a.pdf": 7_777})
    (hit,) = await other.run(cache=rig.cache)
    assert (hit.name, hit.size, hit.outcome) == ("copy of a.pdf", 7_777, "text") and other.parser.requests == []


async def test_a_password_pdf_is_locked_cached_and_not_retried():
    rig = Rig({"MP-2026-118204.pdf": pdf("p")}, parser=ScriptedParser({}, default=LOCKED))
    (att,) = await rig.run()
    assert att.read is AttachmentRead.LOCKED and rig.sleeps == [] and len(rig.parser.requests) == 1
    assert len(rig.cache) == 1


async def test_an_encrypted_docx_is_locked_too():
    rig = Rig({"letter.docx": zipped("word/document.xml")}, parser=ScriptedParser({}, default=LOCKED))
    (att,) = await rig.run()
    assert att.read is AttachmentRead.LOCKED


async def test_a_scan_is_marked_not_read_and_the_reader_never_runs():
    rig = Rig({"birth-certificate.pdf": pdf("s")}, parser=ScriptedParser({}, default=SCAN))
    (att,) = await rig.run()
    assert att.outcome == "scan_not_read" and rig.reader.requests == []


async def test_busy_then_text_sleeps_two_then_five():
    parser = ScriptedParser({"a.pdf": [ParserBusy(), ParserBusy(), text_out()]})
    rig = Rig({"a.pdf": pdf("a")}, parser=parser)
    (att,) = await rig.run()
    assert att.read is AttachmentRead.TEXT and rig.sleeps == [2.0, 5.0] and len(parser.requests) == 3


async def test_busy_forever_raises_and_caches_nothing():
    rig = Rig({"a.pdf": pdf("a")}, parser=ScriptedParser({"a.pdf": ParserBusy()}))
    with pytest.raises(AttachmentParserUnavailable) as exc:
        await rig.run()
    assert exc.value.reason == "busy" and rig.cache == {} and rig.sleeps == [2.0, 5.0, 10.0]


async def test_the_busy_back_off_never_passes_the_deadline():
    rig = Rig({"a.pdf": pdf("a")}, parser=ScriptedParser({"a.pdf": ParserBusy()}))
    with pytest.raises(AttachmentParserUnavailable):
        await rig.run(deadline=rig.now + 1.0)
    assert rig.sleeps == []


async def test_an_unavailable_parser_raises_at_once():
    rig = Rig({"a.pdf": pdf("a")}, parser=ScriptedParser({"a.pdf": ParserUnavailable("unavailable")}))
    with pytest.raises(AttachmentParserUnavailable) as exc:
        await rig.run()
    assert exc.value.reason == "unavailable" and rig.sleeps == [] and rig.cache == {}


async def test_mailbox_errors_propagate():
    rig = Rig({"a.pdf": pdf("a")})
    rig.mailbox.fail_with = EmailPartUnavailable("429")
    with pytest.raises(EmailPartUnavailable):
        await rig.run()
    rig.mailbox.fail_with = None
    rig.mailbox.messages.clear()
    with pytest.raises(EmailMessageGone):
        await rig.run()


async def test_a_text_result_carries_full_text_and_the_cut_input():
    long_text = "\n\n".join(f"Section {i}. " + "word " * 50 for i in range(80))
    rig = Rig({"a.pdf": pdf("a")}, parser=ScriptedParser({}, default=text_out(long_text)))
    (att,) = await rig.run()
    assert att.text == long_text and att.kind == "pdf"
    assert 0 < len(att.head) <= ATTACHMENT_HEAD_CHARS and 0 < len(att.tail) <= ATTACHMENT_TAIL_CHARS


async def test_a_sheet_uses_the_sheet_rule():
    rows = "\n".join(f"| {i} | item {i} |" for i in range(40))
    sheet = f"## Budget\n| n | item |\n| --- | --- |\n{rows}"
    rig = Rig({"budget_2026.xlsx": zipped("xl/workbook.xml")}, parser=ScriptedParser({}, default=text_out(sheet)))
    (att,) = await rig.run()
    assert att.kind == "xlsx" and att.tail == "" and att.text == sheet
    assert "item 19 |" in att.head and "item 25 |" not in att.head


async def test_blank_text_is_no_text():
    rig = Rig({"a.docx": zipped("word/document.xml")}, parser=ScriptedParser({}, default=text_out("   \n ")))
    assert by_name(await rig.run()) == {"a.docx": "skipped:no_text"}


@pytest.mark.parametrize("outcome,expected", [
    (ExtractionOutcome(status=ExtractionStatus.TOO_LARGE), "skipped:too_large"),
    (ExtractionOutcome(status=ExtractionStatus.MEMORY), "skipped:unreadable"),
    (ExtractionOutcome(status=ExtractionStatus.ERROR, detail="broken"), "skipped:unreadable"),
    (ExtractionOutcome(status=ExtractionStatus.TIMEOUT, detail="child wall clock"), "skipped:timeout"),
])
async def test_the_file_own_failures_are_skips_and_are_cached(outcome, expected):
    rig = Rig({"a.docx": zipped("word/document.xml")}, parser=ScriptedParser({}, default=outcome))
    assert by_name(await rig.run()) == {"a.docx": expected}


async def test_an_empty_part_and_an_oversized_download_are_skips():
    rig = Rig({"a.pdf": pdf("a"), "b.pdf": pdf("b")})
    rig.mailbox.blobs["H0"] = b""

    original = rig.mailbox.get_part

    async def too_big(credentials, message_id, part, max_bytes):
        if part.filename == "b.pdf":
            raise EmailPartTooLarge(max_bytes)
        return await original(credentials, message_id, part, max_bytes)

    rig.mailbox.get_part = too_big  # type: ignore[method-assign]
    assert by_name(await rig.run()) == {"a.pdf": "skipped:empty", "b.pdf": "skipped:too_large"}
    assert rig.parser.requests == []


async def test_a_listed_size_over_the_cap_is_never_downloaded():
    rig = Rig({"a.pdf": pdf("a")}, sizes={"a.pdf": 6 * 1024 * 1024})
    assert by_name(await rig.run()) == {"a.pdf": "skipped:too_large"} and rig.mailbox.downloads == []


async def test_a_deadline_already_passed_raises_and_downloads_nothing():
    rig = Rig({"a.pdf": pdf("a")}, now=500.0)
    with pytest.raises(AttachmentParserUnavailable) as exc:
        await rig.run(deadline=500.0)
    assert exc.value.reason == "deadline" and rig.mailbox.downloads == []


async def test_the_document_services_own_deadline_raises_deadline_and_is_not_cached():
    parser = BlockingParser()
    rig = Rig({"a.pdf": pdf("a")}, parser=parser)
    with pytest.raises(AttachmentParserUnavailable) as exc:
        await rig.run(deadline=time.time() + 0.05)
    assert exc.value.reason == "deadline" and rig.cache == {}


async def test_a_parser_wall_clock_timeout_is_the_files_own_skip():
    rig = Rig({"a.pdf": pdf("a")},
              parser=ScriptedParser({"a.pdf": ExtractionOutcome(status=ExtractionStatus.TIMEOUT, detail="wall clock")}))
    assert by_name(await rig.run()) == {"a.pdf": "skipped:timeout"}


class BlockingParser(ContentExtractorPort):
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.release = asyncio.Event()
        self.two_in_flight = asyncio.Event()

    @property
    def extractor_id(self) -> str:
        return "parser"

    @property
    def version(self) -> int:
        return 1

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight >= 2:
            self.two_in_flight.set()
        await self.release.wait()
        self.in_flight -= 1
        return text_out()


async def test_at_most_two_parser_calls_are_in_flight_per_service():
    parser = BlockingParser()
    rig = Rig({f"d{i}.pdf": pdf(str(i)) for i in range(3)}, parser=parser)
    tasks = [asyncio.create_task(rig.run(cache={})) for _ in range(4)]
    # The path to the parser crosses asyncio.to_thread in the document service, so no fixed number of loop
    # turns guarantees arrival: wait for the condition, then give a leaking semaphore time to let a third in.
    await asyncio.wait_for(parser.two_in_flight.wait(), timeout=5)
    await asyncio.sleep(0.2)
    assert parser.in_flight == 2          # the gather really overlapped, and the semaphore held the rest back
    parser.release.set()
    results = await asyncio.gather(*tasks)
    assert parser.max_in_flight == 2 and all(len(r) == 3 for r in results)
