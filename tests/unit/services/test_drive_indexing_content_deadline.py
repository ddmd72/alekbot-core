"""A file that cannot finish before the tick deadline never starts (USER_DRIVE_INDEXING_RFC §7.1, §24.3, §17.5)."""
from datetime import timedelta

import pytest

from src.domain.drive_index import RecordState
from src.domain.drive_index_params import (
    DOWNLOAD_TIMEOUT_S, EMBED_FILE_BUDGET_S, EXTRACT_WALL_TIMEOUT_S, FILE_START_MARGIN_S, MODEL_FILE_BUDGET_S,
    TICK_DEADLINE_S, TICK_START_BUDGET,
)
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

PARSED_WORST = DOWNLOAD_TIMEOUT_S + EXTRACT_WALL_TIMEOUT_S + EMBED_FILE_BUDGET_S + FILE_START_MARGIN_S
ROUTED_WORST = PARSED_WORST + MODEL_FILE_BUDGET_S


def test_the_configured_budgets_fit_the_deadline():
    """§24.3: a routed file started at the very end of the start budget still ends before the deadline."""
    assert TICK_START_BUDGET.total_seconds() + ROUTED_WORST < TICK_DEADLINE_S


async def slow_first_file(w, seconds):
    """The first extraction of the tick takes `seconds`; the rest are instant."""
    state = {"done": False}

    def before(request):
        if not state["done"]:
            state["done"] = True
            w.clock.advance(seconds)
    w.parser.before = before


async def test_no_file_starts_after_the_start_budget():
    w = ContentWorld()
    first = w.add_doc("a.md", prose(5))
    second = w.add_doc("b.md", prose(5))
    await w.settle()   # onboarding indexes both
    w.drive.edit(first, prose(6).encode(), modified_at=w.clock.now - timedelta(hours=1))
    w.drive.edit(second, prose(7).encode(), modified_at=w.clock.now - timedelta(hours=1))
    await slow_first_file(w, TICK_START_BUDGET.total_seconds() + 1)
    downloads = len(w.drive.downloads)
    await w.tick()
    assert len(w.drive.downloads) == downloads + 1   # the second edit waits for the next tick
    assert any(":cont:" in (t["key"] or "") for t in w.queue.tasks[-1:])
    await w.settle()
    assert w.rec(second).indexed_version == w.rec(second).content_version


async def test_a_routed_file_waits_while_a_parsed_one_still_fits(monkeypatch):
    """With the start budget lifted, the deadline rule alone decides (§24.3): after `elapsed` seconds a PDF (it may
    reach the reader) no longer fits, a Markdown file still does."""
    monkeypatch.setattr(svc_mod, "TICK_START_BUDGET", timedelta(hours=1))
    w = ContentWorld()
    slow = w.add_doc("a_slow.md", prose(5))
    await w.settle()
    pdf = w.add_doc("b.pdf", prose(5))
    md = w.add_doc("c.md", prose(5))
    w.drive.edit(slow, prose(6).encode(), modified_at=w.clock.now - timedelta(hours=1))
    elapsed = TICK_DEADLINE_S - ROUTED_WORST + 1   # a routed file would end past the deadline
    assert elapsed + PARSED_WORST < TICK_DEADLINE_S
    await slow_first_file(w, elapsed)
    await w.tick()
    assert pdf not in w.drive.downloads and md in w.drive.downloads
    assert w.rec(pdf).state is RecordState.PENDING and w.rec(pdf).attempt_started_at is None
    await w.settle()
    assert w.rec(pdf).state is RecordState.INDEXED


async def test_the_file_budget_bounds_runs_per_tick(monkeypatch):
    monkeypatch.setattr(svc_mod, "TICK_FILE_BUDGET", 2)
    w = ContentWorld()
    for i in range(5):
        w.add_doc(f"d{i}.md", prose(3))
    await w.tick()
    assert len(w.parser.requests) == 2
    await w.settle()
    assert len(w.parser.requests) == 5
