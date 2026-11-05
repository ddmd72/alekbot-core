"""U5 frozen core: `reader_family` pairs with `extracted_by` in the planner. A record that enters metadata_only from
metadata (a rename away) loses both; a content demotion that stands keeps both."""
from dataclasses import replace

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionOutcome
from src.domain.drive_index import Reason, RecordState
from src.domain.drive_index_planner import demoted, generation_started

from .test_drive_index_planner_content import (  # noqa: F401  (fixture)
    NOW, admitted, carded, ch, indexed_rec, observe, stored,
)

pytestmark = pytest.mark.usefixtures("admitted")


def test_metadata_only_from_metadata_drops_the_family_with_the_stamp():
    rec = replace(indexed_rec(), reader_family="pdf")
    away = observe(ch("contract.pdf.bak"), rec)
    assert (away.state, away.reason) == (RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED)
    assert (away.extracted_by, away.reader_family) == (None, "")


def test_a_content_demotion_that_stands_keeps_its_family():
    rec = carded(demoted(generation_started(stored(ch()), NOW), Reason.NO_TEXT, NOW,
                         outcome=ExtractionOutcome(status=ExtractionStatus.NO_TEXT, extracted_by="parser@3",
                                                   reader_family="pdf")))
    renamed = observe(ch("renamed.pdf"), rec)
    assert (renamed.state, renamed.extracted_by, renamed.reader_family) == (RecordState.METADATA_ONLY, "parser@3",
                                                                            "pdf")
