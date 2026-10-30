"""Image triage in the tick (USER_DRIVE_INDEXING_RFC §5.3, §24.5, §24.7, §24.8, §16 Slice 4 photo invariant). Search
v2: a photograph's description (§25.4.3) replaces the caption, and the image budget is triage's dollars (§25.4.5)."""
import math
from datetime import datetime, timezone

import pytest

from src.domain.drive_card import card_text
from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageReply
from src.domain.drive_index import Priority, Reason, RecordState, TriageVerdict
from src.domain.drive_enrichment import UsageClass
from src.domain.drive_index_params import (
    MAX_ATTEMPTS, QUIET, RETRY_CAP, TRIAGE_BATCH, TRIAGE_IMAGE_USD_ESTIMATE, TRIAGE_THUMBNAIL_SIZE, TRIAGE_USD_PER_MONTH,
)
from src.domain.user_drive import DriveAccessDenied

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_usage_helpers import close_initial_build, month_usd, spend
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


@pytest.mark.slow
@pytest.mark.parametrize("photos", [100, 16, 17, 250])
async def test_holiday_photos_cost_only_batched_triage(w, photos):
    """§16: N photos, camera originals and messenger copies without EXIF, cost ⌈N / TRIAGE_BATCH⌉ triage calls and
    no reader call."""
    folder = w.drive.add("Holiday", folder=True)
    ids = [w.add_image(f"IMG_{i:04d}.jpg", parent=folder, camera=i % 2 == 0) for i in range(photos)]
    w.triage.verdicts = {i: (TriageVerdict.PHOTO, f"a beach at dusk {n}") for n, i in enumerate(ids)}
    w.triage.cost_per_call = 0.002
    await w.settle()
    assert len(w.triage.calls) == math.ceil(photos / TRIAGE_BATCH)
    assert all(len(batch) <= TRIAGE_BATCH for batch in w.triage.calls)
    assert w.reader.requests == [] and w.drive.downloads == []
    assert {size for _, size in w.drive.thumbnail_calls} == {TRIAGE_THUMBNAIL_SIZE}
    for n, item in enumerate(ids):
        rec = w.rec(item)
        assert (rec.state, rec.reason, rec.photo_description) == (RecordState.METADATA_ONLY, Reason.PHOTOGRAPH,
                                                                  f"a beach at dusk {n}")
        assert rec.has_card and rec.photo_description in card_text(rec)
    card_calls = [t for t in w.emb.calls if t.startswith("IMG_")]
    assert len(card_calls) == photos   # each photo is carded once, after its description
    assert w.summary_total("triage_images") == photos
    assert await month_usd(w.repo, UsageClass.TRIAGE, "2026-10") == pytest.approx(
        0.002 * math.ceil(photos / TRIAGE_BATCH))   # only the batched calls are paid


async def test_the_camera_prior_travels_with_each_thumbnail(w):
    camera = w.add_image("a.jpg", camera=True)
    messenger = w.add_image("b.jpg")
    await w.settle()
    sent = {image.item_id: image for batch in w.triage.calls for image in batch}
    assert sent[camera].camera_prior and not sent[messenger].camera_prior
    assert sent[camera].mime_type == "image/jpeg" and sent[camera].thumbnail.startswith(b"\xff\xd8")


async def test_a_text_verdict_sends_the_image_to_the_reader(w):
    from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
    receipt = w.add_image("IMG_2.jpg")
    w.triage.verdicts = {receipt: (TriageVerdict.TEXT, "")}
    w.reader.outcomes["IMG_2.jpg"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Total 12.50 EUR",
                                                       pages_total=1, pages_sent=1)
    await w.settle()
    rec = w.rec(receipt)
    assert (rec.state, rec.triage_verdict, rec.photo_description) == (RecordState.INDEXED, TriageVerdict.TEXT, "")
    assert len(w.reader.requests) == 1 and w.readable(receipt)


async def test_no_thumbnail_is_a_photograph_without_a_caption(w):
    # review B minor: a missing rendition may not be made yet, so it is retried with backoff and becomes a
    # photograph without a caption only at MAX_ATTEMPTS
    img = w.add_image("odd.jpg", thumbnail=None)
    for attempt in range(1, MAX_ATTEMPTS):
        await w.settle()
        assert (w.rec(img).state, w.rec(img).attempts, w.rec(img).triage_verdict) == (RecordState.PENDING, attempt, None)
        w.clock.advance(int(RETRY_CAP.total_seconds()))
    await w.settle()
    rec = w.rec(img)
    assert (rec.reason, rec.photo_description, rec.triage_verdict) == (Reason.PHOTOGRAPH, "", TriageVerdict.PHOTO)
    assert rec.state is RecordState.METADATA_ONLY and rec.due_at is None
    assert w.triage.calls == [] and [i for i, _ in w.drive.thumbnail_calls] == [img] * MAX_ATTEMPTS


async def test_a_thumbnail_error_backs_off_then_fails(w):
    img = w.add_image("a.jpg")
    for _ in range(3):
        w.drive.download_errors.clear()

        async def denied(user_id, item_id, size):
            raise DriveAccessDenied("no")
        w.drive.get_thumbnail = denied
        await w.settle()
        w.clock.advance(6 * 3600)
    rec = w.rec(img)
    assert (rec.state, rec.reason) == (RecordState.FAILED, Reason.DOWNLOAD_ERROR) and w.triage.calls == []


async def test_a_missing_result_is_a_per_file_failure(w):
    """An image the reply leaves unsettled (no result, not capped, not refused: triage v2, §25.13.1) is retried."""
    img = w.add_image("a.jpg")

    async def silent(user_id, account_id, images):
        w.triage.calls.append(list(images))
        return TriageReply(status=ModelReplyStatus.INVALID, results=(), model="m", usd=0.0, model_calls=1)
    w.triage.triage = silent
    await w.settle()
    rec = w.rec(img)
    assert (rec.state, rec.attempts, rec.triage_verdict) == (RecordState.PENDING, 1, None)


async def test_a_triage_outage_defers_images_and_lets_documents_through(w):
    w.triage.unavailable = True
    img = w.add_image("a.jpg")
    doc = w.add_doc("a.md", prose(5))
    result = await w.settle()
    assert result.status == "done" and w.rec(doc).state is RecordState.INDEXED
    rec = w.rec(img)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert (rec.state, rec.triage_verdict, rec.attempts) == (RecordState.PENDING, None, 0)
    assert rec.has_card and state.model_backoff_until == rec.due_at
    w.triage.unavailable = False
    w.clock.now = state.model_backoff_until
    await w.settle()
    assert w.rec(img).reason is Reason.PHOTOGRAPH


async def test_spent_images_wait_for_the_next_month(w):
    await w.settle()
    await close_initial_build(w.repo, w.clock.now)   # the month's cap is what pays: two images' worth left
    await spend(w.repo, UsageClass.TRIAGE, TRIAGE_USD_PER_MONTH - 2 * TRIAGE_IMAGE_USD_ESTIMATE, w.clock.now)
    ids = [w.add_image(f"p{i}.jpg") for i in range(5)]
    await w.settle()
    triaged = [i for i in ids if w.rec(i).triage_verdict is not None]
    waiting = [w.rec(i) for i in ids if w.rec(i).triage_verdict is None]
    assert len(triaged) == 2 and len(waiting) == 3
    november = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert all((r.due_at, r.priority) == (november, Priority.MAINTENANCE) for r in waiting)
    w.clock.now = november
    await w.settle()
    assert all(w.rec(i).reason is Reason.PHOTOGRAPH for i in ids)


async def test_a_renamed_photo_keeps_its_verdict(w):
    img = w.add_image("a.jpg")
    await w.settle()
    w.drive.rename(img, "beach.jpg")
    await w.settle()
    assert len(w.triage.calls) == 1 and w.rec(img).photo_description == "a photo"


async def test_new_content_is_triaged_again(w):
    img = w.add_image("a.jpg")
    await w.settle()
    w.drive.edit(img, b"\xff\xd8\xff\xe1 edited", modified_at=w.clock.now)
    await w.settle()
    assert len(w.triage.calls) == 1   # the quiet period holds an edit (§7.3)
    w.clock.advance(QUIET.total_seconds())
    await w.settle()
    assert len(w.triage.calls) == 2
