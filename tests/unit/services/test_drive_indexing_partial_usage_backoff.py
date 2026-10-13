"""Usage spent before an outage never ends the outage (USER_DRIVE_INDEXING_RFC §24.7, §24.8; round-2 review T-M2a): the
pages a file's windows answered are counted against the month, and the outage that follows still doubles the model
backoff. Resetting it with the usage let a dense PDF re-read its finished windows every base backoff."""
import pytest

from src.domain.drive_extraction import DriveModelUnavailable, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index_params import MODEL_BACKOFF_BASE_S

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


def _outage(calls, **usage):
    def raise_it(request):
        calls.append(request)
        raise DriveModelUnavailable("window 3 failed", **usage)
    return raise_it


async def test_a_reader_outage_after_partial_usage_keeps_doubling():
    w = ContentWorld()
    w.parser.outcomes["long.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=40)
    calls = []
    w.reader.before = _outage(calls, pages_sent=20, model_calls=2, model_cost=0.002)
    w.add_doc("long.pdf", "")
    await w.settle()
    first = await w.repo.get_sync_state("u")
    assert first.model_failures == 1
    w.clock.advance(MODEL_BACKOFF_BASE_S)
    await w.settle()
    second = await w.repo.get_sync_state("u")
    assert len(calls) == 2 and second.model_pages_used == 40
    assert second.model_failures == 2
    assert (second.model_backoff_until - w.clock.now).total_seconds() == pytest.approx(2 * MODEL_BACKOFF_BASE_S,
                                                                                       abs=5)


async def test_a_triage_outage_after_partial_usage_keeps_doubling():
    w = ContentWorld()

    async def partial(user_id, account_id, images):
        raise DriveModelUnavailable("429 on the second half", images_sent=1, model_calls=1)

    w.triage.triage = partial
    w.add_image("p.jpg")
    await w.settle()
    w.clock.advance(MODEL_BACKOFF_BASE_S)
    await w.settle()
    state = await w.repo.get_sync_state("u")
    assert state.triage_images_used == 2 and state.model_failures == 2
