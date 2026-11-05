"""A parser VERSION bump (the upgrade pass) never touches a transcript record and never causes a paid call: the
transcript is built by the transcript stage as production does, then the indexer is redeployed with parser@2."""
import pytest

from src.domain.document_extraction import PARSER, READER
from src.domain.drive_extraction import TRANSCRIBE_LINK
from src.domain.drive_index import RecordState
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_world import HookedExtractor, admitted  # noqa: F401  (fixture)
from .drive_index_fakes import LegacyConnections
from .test_drive_indexing_transcripts import SPACE, world  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


def paid(w):
    return (len(w.runner.submits), len(w.batch.uploads), len(w.batch.polls), len(w.media.probes), len(w.media.runs),
            len(w.urls.calls), w.reserved(), len(w.batches()))


async def test_a_parser_bump_leaves_a_transcribed_record_indexed_and_makes_no_paid_call(world):
    w = world()
    memo = w.add_recording()
    w.add_doc("notes.md", "Flamingo fence notes. " * 40)
    await w.to_current(memo)
    before, spent = w.rec(memo), paid(w)
    assert before.state is RecordState.INDEXED and before.extracted_by.startswith(f"{TRANSCRIBE_LINK}@")
    parser = HookedExtractor(PARSER, version=2)
    w.parser = parser
    w.svc = DriveIndexingService(
        w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: "", SPACE,
        extractors={PARSER: parser, READER: w.reader}, triage=w.triage, transcripts=w.stage, transcribe_enabled=True,
        connections=LegacyConnections(w.users, w.drive), clock=w.clock, monotonic=w.clock.monotonic)
    await w.settle()
    assert any(r.name == "notes.md" for r in parser.requests)   # the pass did run, for the parser's own record
    after = w.rec(memo)
    assert (after.state, after.extracted_by, after.extraction_version) == (
        before.state, before.extracted_by, before.extraction_version)
    assert after.indexed_generation == before.indexed_generation and after.transcript_state == "current"
    assert not any(r.name == "memo.m4a" for r in parser.requests)
    assert paid(w) == spent   # nothing uploaded, submitted, polled or reserved
