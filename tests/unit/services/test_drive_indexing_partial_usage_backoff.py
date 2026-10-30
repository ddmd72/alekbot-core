"""Usage spent before an outage never ends the outage (USER_DRIVE_INDEXING_RFC §24.7, §24.8; round-2 review T-M2a): the
pages a file's windows answered are counted against the month, and the outage that follows still doubles the model
backoff. Resetting it with the usage let a dense PDF re-read its finished windows every base backoff. Search v2
(§25.4.5): what was spent is counted in dollars in the month's usage document."""
import pytest

from src.domain.drive_enrichment import PhotoDescription, UsageClass
from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageReply, TriageResultV2
from src.domain.drive_extraction import DriveModelUnavailable, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import TriageVerdict
from src.domain.drive_index_params import MODEL_BACKOFF_BASE_S

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)
from .drive_usage_helpers import month_calls, month_usd
from .drive_index_fakes import U_SCOPE

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
    first = await w.repo.get_sync_state(U_SCOPE)
    assert first.model_failures == 1
    w.clock.advance(MODEL_BACKOFF_BASE_S)
    await w.settle()
    second = await w.repo.get_sync_state(U_SCOPE)
    assert len(calls) == 2 and await month_usd(w.repo, UsageClass.READER, "2026-10") == pytest.approx(0.004)
    assert w.summary_total("model_pages") == 40
    assert second.model_failures == 2
    assert (second.model_backoff_until - w.clock.now).total_seconds() == pytest.approx(2 * MODEL_BACKOFF_BASE_S,
                                                                                       abs=5)


async def test_a_triage_outage_after_partial_usage_keeps_doubling():
    w = ContentWorld()

    async def partial(user_id, account_id, images):
        # triage v2 (§25.13.1): the outage is the reply's status; the first image was answered before it
        first = TriageResultV2(item_id=images[0].item_id, verdict=TriageVerdict.PHOTO,
                               photo=PhotoDescription(description="a photo"))
        return TriageReply(status=ModelReplyStatus.UNAVAILABLE, results=(first,), model="m", usd=0.001,
                           model_calls=1)

    w.triage.triage = partial
    for n in range(3):   # one answered per outage, so the second tick still has an image to triage
        w.add_image(f"p{n}.jpg")
    await w.settle()
    w.clock.advance(MODEL_BACKOFF_BASE_S)
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.model_failures == 2 and w.summary_total("triage_images") == 2
    assert await month_usd(w.repo, UsageClass.TRIAGE, "2026-10") == pytest.approx(0.002)
    assert await month_calls(w.repo, "2026-10") == 2
