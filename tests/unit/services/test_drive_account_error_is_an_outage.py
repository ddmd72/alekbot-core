"""An account-level failure is a model outage, never a refusal of the input (USER_DRIVE_INDEXING_RFC §24.14, §24.8).

Anthropic answers an exhausted credit balance with HTTP 400, the status the indexer reads as "the provider refused
this input". Read that way, a credit lapse refused every page of every scan one by one and demoted the file to
`unsupported` for good, demoted every image the same way, and split triage batches down to captionless photos —
with nothing retried until an edit. The adapter recognises it (LLMAccountError); the indexer treats it like a 429:
model work is deferred for the user, no file is charged, the file stays pending. An ordinary 400 is still a refusal."""
import pytest

from src.domain.drive_extraction import DriveModelUnavailable, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS
from src.domain.exceptions import LLMAccountError, LLMClientError
from src.infrastructure.drive_model_call import DriveInputRefused, DriveModelCallFailed, classify_model_error
from tests.unit.agents.test_drive_image_triage_agent import _agent, _images
from tests.unit.composition.test_drive_reader_runner_halving import _request, _runner
from tests.unit.composition.test_drive_reader_runner_refusals_terminal import _image

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

CREDIT = LLMAccountError("Error code: 400 - credit balance is too low", http_status=400)


def test_a_credit_balance_400_is_an_outage_that_charges_no_file():
    mapped = classify_model_error(CREDIT, "drive_document_reader")
    assert isinstance(mapped, DriveModelUnavailable) and not isinstance(mapped, DriveModelCallFailed)
    assert mapped.charges_file is False


def test_an_ordinary_400_is_still_a_refusal_of_the_input():
    assert isinstance(classify_model_error(LLMClientError("bad image", http_status=400), "drive_reader"),
                      DriveInputRefused)


async def test_a_scan_is_not_read_page_by_page_into_unsupported(tmp_path):
    runner, llm, _ = _runner(lambda r: CREDIT)
    with pytest.raises(DriveModelUnavailable) as raised:
        await runner.extract(_request(tmp_path))
    assert raised.value.charges_file is False
    assert len(llm.requests) <= 3                     # the windows in flight, never halved down to single pages


async def test_an_image_is_not_demoted_unsupported(tmp_path):
    runner, _, _ = _runner(lambda r: CREDIT)
    with pytest.raises(DriveModelUnavailable) as raised:
        await runner.extract(_image(tmp_path))
    assert raised.value.charges_file is False


async def test_a_triage_batch_is_not_split():
    agent, llm = _agent([CREDIT])
    with pytest.raises(DriveModelUnavailable):
        await agent.classify(_images(4), account_id="acc")
    assert len(llm.requests) == 1


@pytest.mark.usefixtures("admitted")
async def test_a_credit_lapse_leaves_the_file_pending_and_uncharged():
    w = ContentWorld()
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=30)

    def lapsed(request):
        raise classify_model_error(CREDIT, "drive_document_reader")

    w.reader.before = lapsed
    doc = w.add_doc("scan.pdf", "")
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(4 * 3600)
    rec = w.rec(doc)
    assert (rec.state, rec.model_outages, rec.attempts) == (RecordState.PENDING, 0, 0)
