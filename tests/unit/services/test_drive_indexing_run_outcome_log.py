"""On-call can answer "why was file X not indexed" from the logs (final review F4-6; USER_DRIVE_INDEXING_RFC §12.1): one
INFO line per index-run outcome names the item id, the record's state and reason, the extractor that decided and the
pages, refused pages and truncation; the reader's own line names the item id. No document text and no file name."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.drive_reader_runner import DriveReaderRunner
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ExtractionStatus
from src.domain.user import UserBotConfig
from tests.unit.agents.drive_model_fakes import RoutedLLM, context, prompt_builder, reply

from .drive_content_world import JPEG, ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

SECRET = "Confidential clause about the tenant"


def _run_lines(caplog, item_id):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("[DriveIndex] run ") and f"item={item_id} " in r.getMessage()]


async def test_an_indexed_run_logs_its_outcome(caplog):
    caplog.set_level(logging.INFO)
    w = ContentWorld()
    doc = w.add_doc("lease-contract.md", f"{SECRET}. " + prose(20))
    await w.settle()
    (line,) = _run_lines(caplog, doc)
    assert "state=indexed" in line and "reason=-" in line and "extractor=parser@1" in line
    assert "truncated=False" in line and "user=u " in line
    assert SECRET not in line and "lease-contract" not in line


async def test_a_reader_run_logs_pages_refused_and_truncation(caplog):
    caplog.set_level(logging.INFO)
    w = ContentWorld()
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=12)
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(
        status=ExtractionStatus.TEXT, text="Total 12.50 EUR. " * 20, pages_total=12, pages_sent=11, model_calls=3,
        pages_refused=1, truncated=True)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    (line,) = _run_lines(caplog, doc)
    assert "state=indexed" in line and "extractor=reader@1" in line
    assert "pages=11/12" in line and "refused=1" in line and "truncated=True" in line


async def test_a_demoted_run_logs_its_reason(caplog):
    caplog.set_level(logging.INFO)
    w = ContentWorld()
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=3)
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.UNSUPPORTED, pages_total=3,
                                                      pages_sent=3, pages_refused=3, extracted_by="reader@1")
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    (line,) = _run_lines(caplog, doc)
    assert "state=metadata_only" in line and "reason=" in line and "reason=-" not in line
    assert "refused=3" in line and "extractor=reader@1" in line


async def test_a_retried_run_logs_a_line_per_attempt(caplog):
    caplog.set_level(logging.INFO)
    w = ContentWorld()
    w.parser.outcomes["broken.docx"] = ExtractionOutcome(status=ExtractionStatus.ERROR)
    doc = w.drive.add_file("broken.docx", b"PK\x03\x04")
    await w.settle()
    lines = _run_lines(caplog, doc)
    assert lines and "state=pending" in lines[0] and "outcome=retried" in lines[0]


async def test_the_reader_line_names_the_item(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    llm = RoutedLLM(lambda request: reply({"text": "Total 12.50 EUR", "brief": None}))
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    runner = DriveReaderRunner(contexts, users, prompt_builder(), MagicMock())
    path = tmp_path / "img"
    path.write_bytes(JPEG)
    await runner.extract(ExtractionRequest(path=str(path), name="receipt.jpg", kind="image", mime_type="image/jpeg",
                                           user_id="user-1", account_id="acc-1", max_model_pages=10,
                                           item_id="ITEM-42"))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveReader] ")]
    assert "item=ITEM-42" in line and "receipt" not in line
