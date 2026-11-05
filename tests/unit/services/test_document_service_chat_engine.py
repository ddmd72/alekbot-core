"""A3 (EXTERNAL_FILES_RFC §4.4): a `text/*` CSV or HTML opened in chat goes to the parser child like every other parse
kind; text and Markdown stay in process; mhtml stays raw. Invented cast."""
from src.domain.document_read import ReadStatus
from tests.unit.services.test_document_service_read import make, req

BUDGET_CSV = b"item,amount\nrent,11400\ngroceries,4200\ntransport,960\nholidays,2500\n"
MEETING_HTML = b"<html><body><h1>Example School parents' meeting</h1><p>March 3, 2026</p></body></html>"


async def test_a_text_csv_open_reaches_the_parser_and_comes_back_as_its_text():
    service, parser, *_ = make()
    out = await service.read(req(BUDGET_CSV, "budget_2026.csv", "text/csv"))
    assert [r.kind for r in parser.requests] == ["csv"]
    assert out.status is ReadStatus.TEXT and out.text == parser.answer.text and out.links == ("parser@3",)


async def test_a_text_html_open_reaches_the_parser_and_comes_back_as_its_text():
    service, parser, *_ = make()
    out = await service.read(req(MEETING_HTML, "parents_meeting.html", "text/html"))
    assert [r.kind for r in parser.requests] == ["html"]
    assert out.status is ReadStatus.TEXT and out.text == parser.answer.text


async def test_text_plain_and_markdown_stay_in_process():
    service, parser, *_ = make()
    note = "# gym pass\nGym pass (Taras) 360 EUR\n".encode()
    plain = await service.read(req(note, "gym_pass_uk.txt", "text/plain"))
    markdown = await service.read(req(note, "gym_pass_uk.md", "text/markdown"))
    assert plain.text == markdown.text == note.decode() and parser.requests == []


async def test_mhtml_stays_raw():
    service, parser, *_ = make()
    body = b"MIME-Version: 1.0\nContent-Type: multipart/related; boundary=x\n\n--x\nContent-Type: text/html\n\nhi\n--x--\n"
    out = await service.read(req(body, "parents_meeting.mhtml", "text/plain"))
    assert parser.requests == [] and out.text == body.decode()
