"""The month's model usage is written as it is spent, not only at the end of the tick (final review F1-4;
USER_DRIVE_INDEXING_RFC §24.7, §24.14): after each reader file and each triage batch, one fenced write of the sync state
carries it. A tick the instance kills (OOM, replacement, SIGTERM) or whose lease is lost keeps the pages and images
already spent counted against the month."""
from datetime import timedelta

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index_params import TICK_DEADLINE_S

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=7)


def _read(pages):
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text="Invoice total 12.50 EUR, paid in full.",
                             pages_total=pages, pages_sent=pages, model_calls=1)


class Killed(BaseException):
    """The instance dies: nothing in the tick catches it."""


def _two_scans(w):
    for name in ("a.pdf", "b.pdf"):
        w.parser.outcomes[name] = SCAN
        w.reader.outcomes[name] = _read(7)
        w.add_doc(name, "")


async def test_a_killed_tick_keeps_the_pages_its_finished_files_spent():
    w = ContentWorld()
    _two_scans(w)

    def second_dies(request):
        if len(w.reader.requests) == 1:   # the first file was read; the instance dies during the second
            raise Killed()

    w.reader.before = second_dies
    with pytest.raises(Killed):
        await w.tick()
    state = await w.repo.get_sync_state("u")
    assert (state.usage_month, state.model_pages_used) == ("2026-10", 7)


async def test_a_lost_lease_keeps_the_pages_written_before_it():
    w = ContentWorld()
    _two_scans(w)

    def lease_runs_out(request):
        if len(w.reader.requests) == 1:   # the tick outlives its lease while the second file is read
            w.clock.now += timedelta(seconds=TICK_DEADLINE_S + 1)

    w.reader.before = lease_runs_out
    result = await w.tick()
    assert result.status == "lease_lost"
    state = await w.repo.get_sync_state("u")
    assert state.model_pages_used == 7   # the first file's; nothing is written once the lease is gone


async def test_a_killed_tick_keeps_the_images_its_triage_batches_spent():
    w = ContentWorld()
    for i in range(3):
        w.add_image(f"photo{i}.jpg")

    async def card_kills(text, task_type="RETRIEVAL_DOCUMENT"):
        raise Killed()

    w.emb.get_embedding = card_kills   # the cards are embedded after the batch: the instance dies there
    with pytest.raises(Killed):
        await w.tick()
    assert len(w.triage.calls) == 1
    state = await w.repo.get_sync_state("u")
    assert state.triage_images_used == 3
