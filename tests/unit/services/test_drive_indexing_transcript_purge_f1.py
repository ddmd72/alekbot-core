"""The purge and a batch whose cost a collection already booked (final-review fix wave F1; T-R22, T-R25): its open
reservation is released, never booked a second time."""
from __future__ import annotations

from dataclasses import replace

import pytest

from .test_drive_indexing_transcript_purge import spent, waiting_world
from .test_drive_indexing_transcripts import admitted, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("admitted")


async def test_an_open_batch_already_booked_is_released_not_booked_again(world):
    w, audio = await waiting_world(world)
    batch = w.batches()[0]
    w.repo.batches[("u", batch.name)] = replace(batch, reserved_usd=0.5, reserved_month="2020-01",
                                                booked_at=w.clock())
    await w.repo.reserve_model_usd("u", "2020-01", "transcribe", 0.5)
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    assert spent(w, "2020-01") == pytest.approx(0.0)
    assert w.repo.usage[("u", "2020-01")].transcribe_reserved_usd == pytest.approx(0.0)


async def test_an_open_batch_not_yet_booked_is_still_booked(world):
    w, audio = await waiting_world(world)
    batch = w.batches()[0]
    w.repo.batches[("u", batch.name)] = replace(batch, reserved_usd=0.5, reserved_month="2020-01")
    await w.repo.reserve_model_usd("u", "2020-01", "transcribe", 0.5)
    w.users.indexing = False
    await w.settle()
    assert spent(w, "2020-01") == pytest.approx(0.5)
