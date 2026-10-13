"""Triage v2 in the tick until P5 (USER_DRIVE_INDEXING_RFC §25.4.4, review m3): an image still stopped at the output
cap alone is retried per file — a text scan must never become a permanent photograph because its description ran
long — while a refused image stays a photograph without a description, as in v1."""
import pytest

from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageReply
from src.domain.drive_index import Reason, RecordState

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


async def test_a_capped_image_is_retried_not_made_a_photograph(w):
    img = w.add_image("scan.jpg")

    async def capped(user_id, account_id, images):
        w.triage.calls.append(list(images))
        return TriageReply(status=ModelReplyStatus.STOPPED_AT_CAP, results=(), model="m", usd=0.0, model_calls=2,
                           capped_item_ids=tuple(i.item_id for i in images))
    w.triage.triage = capped
    await w.settle()
    rec = w.rec(img)
    assert (rec.triage_verdict, rec.state, rec.attempts) == (None, RecordState.PENDING, 1)


async def test_a_refused_image_is_a_photograph_without_a_description(w):
    img = w.add_image("odd.jpg")

    async def refused(user_id, account_id, images):
        w.triage.calls.append(list(images))
        return TriageReply(status=ModelReplyStatus.OK, results=(), model="m", usd=0.0, model_calls=1,
                           refused_item_ids=tuple(i.item_id for i in images))
    w.triage.triage = refused
    await w.settle()
    rec = w.rec(img)
    assert (rec.state, rec.reason, rec.caption) == (RecordState.METADATA_ONLY, Reason.PHOTOGRAPH, "")
