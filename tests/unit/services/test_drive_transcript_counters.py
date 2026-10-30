"""The tick's unread counters and the Cabinet's "waiting" count for recordings (USER_DRIVE_INDEXING_RFC §27.11
"Coverage"; plan rulings G2, G3): recordings count only for a user whose transcription is on, and while it is off they
are taken out of `pending` (no stage runs for them), all with the filters the existing count queries use."""
from unittest.mock import AsyncMock

from src.domain import drive_index_policy as policy
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_search import NOT_READ_YET_COUNTER, UNREAD_COUNTER

from src.domain.drive_handle import DriveHandle

from .drive_index_fakes import U_SCOPE
from .test_drive_search_service import World

# the single-drive harness's connection (`LegacyConnections`: connection id = user id)
HANDLE = DriveHandle(user_id="u", connection_id="u", credential_id="cred", kind="fake")


def rec(item_id, **kw):
    return DriveIndexRecord(**{"user_id": "u", "connection_id": "u", "item_id": item_id, "kind": "audio", "state": RecordState.PENDING, **kw})


async def seeded():
    w = World()
    await w.repo.upsert_records([
        rec("a1"), rec("a2"), rec("v1", kind="video"),
        rec("silent", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT, transcript_stub="no_speech"),
        rec("bad", state=RecordState.FAILED),
        rec("d1", kind="pdf"),
    ])
    return w


async def test_off_the_calls_are_exactly_the_old_ones():
    w = await seeded()
    spy = AsyncMock(wraps=w.repo.count_unread)
    w.repo.count_unread = spy
    out = await w.indexer._unread_counters(HANDLE, {})
    calls = sorted((c.args, tuple(sorted(c.kwargs.items()))) for c in spy.call_args_list)
    assert calls == sorted([
        ((U_SCOPE, policy.ADMITTED_KINDS, policy.TRIAGED_KINDS), (("pending", True),)),
        ((U_SCOPE, policy.ADMITTED_KINDS, policy.TRIAGED_KINDS), (("pending", False),)),
    ], key=lambda c: (c[0], c[1]))
    assert out[NOT_READ_YET_COUNTER] == 1 and out[UNREAD_COUNTER] == 1     # only the pdf


async def test_on_recordings_are_counted():
    w = await seeded()
    out = await w.indexer._unread_counters(HANDLE, {}, transcription_on=True)
    assert out[NOT_READ_YET_COUNTER] == 4                                   # a1, a2, v1, d1
    assert out[UNREAD_COUNTER] == 5                                         # + the failed one; the silent stub is not


async def test_on_passes_the_media_set_to_the_repository():
    w = await seeded()
    spy = AsyncMock(wraps=w.repo.count_unread)
    w.repo.count_unread = spy
    await w.indexer._unread_counters(HANDLE, {}, transcription_on=True)
    assert all(c.kwargs["transcribed"] == policy.TRANSCRIBED_KINDS for c in spy.call_args_list)


async def test_waiting_counter_hides_idle_recordings_while_off():
    w = await seeded()
    off = await w.indexer._counters(HANDLE)
    assert off["pending"] == 1                                              # only the pdf is really waiting
    on = await w.indexer._counters(HANDLE, transcription_on=True)
    assert on["pending"] == 4


async def test_waiting_counter_drops_to_zero_without_other_pending():
    w = World()
    await w.repo.upsert_records([rec("a1"), rec("a2")])
    assert "pending" not in await w.indexer._counters(HANDLE)
