"""A sandbox parser that cannot take the work (ParserUnavailable) is systemic, never the file's: the index run is
abandoned uncounted (attempts unchanged, no model outage recorded), the tick ends, and what the reader had spent is
counted. The README parse re-raises it instead of reading the folder as "no README"."""
import logging

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ParserUnavailable
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_index_params import MAX_ATTEMPTS

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


def _down(reason="unavailable", **spend):
    def hook(request):
        raise ParserUnavailable(reason, **spend)
    return hook


async def test_a_parser_outage_never_charges_the_file_and_ends_the_tick(caplog):
    w = ContentWorld()
    w.parser.before = _down("busy")
    doc = w.add_doc("lease_2026.docx", "Rent is due on the first of the month.")
    with caplog.at_level(logging.WARNING):
        for _ in range(MAX_ATTEMPTS + 2):
            await w.tick()
            w.clock.advance(4 * 3600)
    rec = w.rec(doc)
    assert rec.attempts == 0 and rec.model_outages == 0 and rec.state.value != "failed"
    assert any(r.levelno == logging.WARNING and "parser unavailable (busy)" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR and "failed on item" in r.getMessage() for r in caplog.records)
    w.parser.before = None                         # the parser comes back: the file is indexed as if nothing happened
    await w.settle()
    assert w.rec(doc).attempts == 0 and w.readable(doc)


async def test_a_reader_that_hit_the_parser_outage_counts_its_spend_without_a_model_outage():
    w = ContentWorld()
    w.parser.outcomes["scan_0012.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=30)
    w.reader.before = _down("unavailable", pages_sent=10, model_calls=2, model_cost=0.02)
    doc = w.add_doc("scan_0012.pdf", "")
    await w.tick()
    assert w.summary_total("model_calls") == 2 and w.summary_total("model_pages") == 10
    assert w.rec(doc).attempts == 0 and w.rec(doc).model_outages == 0
    w.reader.before = None
    w.clock.advance(4 * 3600)                       # past the tick's own retry delay, not a model backoff
    await w.tick()
    assert len(w.reader.requests) == 1 and w.readable(doc)


async def test_a_readme_parse_during_a_parser_outage_ends_the_tick_and_is_not_an_empty_readme(caplog):
    w = ContentWorld()
    w.parser.before = _down("unavailable")
    repo = w.drive.add("alek-site", folder=True)
    w.drive.add(".git", parent=repo, folder=True)
    w.drive.add_file("main.py", b"print(1)", parent=repo)
    w.drive.add_file("README.md", b"Alek site: a static site generator.", parent=repo)
    with caplog.at_level(logging.WARNING):
        for _ in range(MAX_ATTEMPTS + 2):
            await w.tick()
            w.clock.advance(4 * 3600)
    assert any(r.levelno == logging.WARNING and "README parse hit an unavailable parser" in r.getMessage()
               for r in caplog.records)
    assert not any("README parse failed" in r.getMessage() for r in caplog.records)
    rec = w.rec(repo)
    assert rec.attempts == 0 and rec.readme_excerpt == ""
    w.parser.before = None
    w.clock.advance(4 * 3600)
    await w.settle()
    assert w.rec(repo).readme_excerpt.startswith("Alek site: a static site generator")
