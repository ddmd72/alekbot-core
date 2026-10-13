"""A photograph's verdict and caption live together (USER_DRIVE_INDEXING_RFC §5.3, §24.5; round-2 review T-m3). An image
renamed to a kind that leaves triage drops its caption, and its verdict goes with it: renamed back, it is triaged again
and its card has a caption once more. Before, the verdict stayed without its caption, nothing re-triaged, and the
caption was lost for good."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_extraction import TriageResult
from src.domain.drive_index import DriveChange, EpochPhase, Reason, TriageVerdict
from src.domain.drive_index_planner import PlanContext, plan_observation, triage_due, triaged

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def test_a_photo_renamed_away_and_back_is_triaged_again():
    ctx = PlanContext(user_id="u", root_item_id="root", epoch=1, phase=EpochPhase.IDLE, now=NOW,
                      place_of=lambda *a: None)
    ch = DriveChange(item_id="i1", parent_id="root", name="beach.jpg", size_bytes=100000, mime_type="image/jpeg",
                     content_version="v1", modified_at=NOW - timedelta(days=3), facets={"image": {"width": 4000}})
    photo = triaged(plan_observation(ch, None, None, None, ctx).upsert,
                    TriageResult(item_id="i1", verdict=TriageVerdict.PHOTO, caption="Sunset over a beach"), NOW)
    away = plan_observation(replace(ch, name="beach.bin", mime_type="application/octet-stream"), photo, None, None,
                            ctx).upsert
    assert (away.caption, away.triage_verdict) == ("", None)
    back = plan_observation(ch, away, None, None, ctx).upsert
    assert triage_due(back) and back.reason is not Reason.PHOTOGRAPH


async def test_the_caption_comes_back_after_a_rename_away_and_back():
    w = ContentWorld()
    img = w.add_image("beach.jpg")
    await w.settle()
    assert w.rec(img).caption == "a photo" and "a photo" in w.card_texts()[img]
    w.drive.rename(img, "beach.zip")   # an archive: no triage
    await w.settle()
    assert w.rec(img).caption == ""
    w.drive.rename(img, "beach.jpg")
    await w.settle()
    rec = w.rec(img)
    assert (rec.reason, rec.caption) == (Reason.PHOTOGRAPH, "a photo") and "a photo" in w.card_texts()[img]
    assert len(w.triage.calls) == 2
