"""One agent type's own enrichment queue (USER_DRIVE_INDEXING_RFC §25.4.5-§25.4.6; review rv_sv2_p5 MINOR 6): with
`job`, `enrich_queue` reads only the records whose job of that name is `ready`, in the queue's order, on both adapters;
each such query has its composite declared for the live collection."""
from dataclasses import replace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, EvalDb, eval_transactions, make_eval_repo
from tests.unit.adapters.test_drive_index_v2_parity import Pair, rec
from tests.unit.adapters.test_firestore_drive_index_v2_provisioning import LIVE, _covered, _declared, _required
from tests.unit.services.drive_index_fakes import U_SCOPE


def _rec(item, **kw):
    """The parity helper's record on the user's one connection (MULTI_DRIVE_RFC §8: a record write needs it)."""
    return replace(rec(item, **kw), connection_id=U_SCOPE.connection_id)


@pytest.fixture
def pair(mock_env_config):
    db = EvalDb()
    with eval_transactions():
        yield db, Pair(InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config))


async def test_a_job_queue_holds_only_that_jobs_ready_records_in_queue_order(pair):
    _, both = pair
    await both.write(records=[
        _rec("a", enrich_ready=True, profile_state="ready", passages_state="current", effective_day="2026-06-01"),
        _rec("b", enrich_ready=True, profile_state="current", passages_state="ready", effective_day="2026-05-01",
            parent_id="p2"),
        _rec("c", enrich_ready=True, profile_state="ready", passages_state="ready", effective_day="2026-05-01",
            parent_id="p1"),
        _rec("d", enrich_ready=False, profile_state="held", passages_state="parked", effective_day="2026-07-01"),
    ])
    assert [r.item_id for r in await both.both("enrich_queue", U_SCOPE, 10)] == ["a", "c", "b"]
    assert [r.item_id for r in await both.both("enrich_queue", U_SCOPE, 10, job="passages")] == ["c", "b"]
    assert [r.item_id for r in await both.both("enrich_queue", U_SCOPE, 10, job="profile")] == ["a", "c"]
    assert [r.item_id for r in await both.both("enrich_queue", U_SCOPE, 1, job="passages")] == ["c"]


async def test_every_job_queue_has_a_declared_index(pair):
    db, both = pair
    for job in ("profile", "passages"):
        await both.fs.enrich_queue(U_SCOPE, 5, job=job)
    declared = _declared(LIVE[RECS])
    queries = [ops for ops in db.cols[RECS].executed if any(op[1] in ("profile_state", "passages_state")
                                                             for op in ops if op[0] == "where")]
    assert len(queries) == 2
    assert all(any(_covered(_required(ops), index) for index in declared) for ops in queries)


async def test_an_unknown_job_is_refused(pair):
    _, both = pair
    for repo in (both.mem, both.fs):
        with pytest.raises(ValueError):
            await repo.enrich_queue(U_SCOPE, 5, job="pages")
