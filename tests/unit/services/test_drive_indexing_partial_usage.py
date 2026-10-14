"""The tick counts what the model spent before an outage (adversarial reviews C-M1 / B-M3; USER_DRIVE_INDEXING_RFC
§24.7-§24.9): DriveModelUnavailable carries pages (or images), calls and cost; the tick applies them to the month and
the summary BEFORE it records the outage, so the backoff still holds."""
import pytest

from src.domain.drive_extraction import DriveModelUnavailable, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import MODEL_BACKOFF_BASE_S

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=120)


@pytest.fixture
def w():
    return ContentWorld()


def _outage(**usage):
    def raise_it(request):
        raise DriveModelUnavailable("429 on window 9", **usage)
    return raise_it


async def test_a_120_page_file_failing_on_window_9_counts_windows_1_to_8(w):
    w.parser.outcomes["long.pdf"] = SCAN
    w.reader.before = _outage(pages_sent=80, model_calls=9, model_cost=0.008)
    doc = w.add_doc("long.pdf", "")
    note = w.add_doc("note.md", prose(5))
    result = await w.settle()
    assert result.status == "done" and w.rec(note).state is RecordState.INDEXED
    state = await w.repo.get_sync_state("u")
    assert (state.usage_month, state.model_pages_used) == ("2026-10", 80)
    assert sum(r.model_cost for r in w.results) == pytest.approx(0.008)
    assert w.summary_total("model_pages") == 80 and w.summary_total("model_calls") == 9
    # the outage is recorded after the usage: the backoff holds and the file waits for it
    rec = w.rec(doc)
    assert state.model_failures == 1 and state.model_backoff_until is not None
    assert (rec.state, rec.due_at) == (RecordState.PENDING, state.model_backoff_until)


async def test_an_outage_with_nothing_spent_keeps_the_doubling_backoff(w):
    w.parser.outcomes["long.pdf"] = SCAN
    w.reader.before = _outage()
    w.add_doc("long.pdf", "")
    await w.settle()
    w.clock.advance(MODEL_BACKOFF_BASE_S)
    await w.settle()
    state = await w.repo.get_sync_state("u")
    assert state.model_failures == 2 and state.model_pages_used == 0   # no usage: the outage is not reset


async def test_a_triage_outage_counts_the_images_answered_before_it(w):
    async def partial(user_id, account_id, images):
        raise DriveModelUnavailable("429 on the second half", images_sent=2, model_calls=3, model_cost=0.003)

    w.triage.triage = partial
    for n in range(4):
        w.add_image(f"p{n}.jpg")
    await w.settle()
    state = await w.repo.get_sync_state("u")
    assert state.triage_images_used == 2 and state.model_backoff_until is not None
    assert sum(r.model_cost for r in w.results) == pytest.approx(0.003)
    assert w.summary_total("triage_calls") == 3 and w.summary_total("triage_images") == 2
