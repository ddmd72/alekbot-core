"""P5: the parity scenarios (tests/support/firestore_parity.py) on H1 always, and on the Firestore emulator when
FIRESTORE_EMULATOR_HOST is set. The same scenarios run on the test database in tests/cloud/.

A backend that disagrees with the documented value is pinned below by a strict xfail with the reason, so the fix of the
backend (H1: a separate, reviewed round) removes the marker. The test database is the arbiter.
"""
import asyncio
import os
import uuid

import pytest
from google.cloud import firestore

from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.support.firestore_parity import SCENARIOS, collection_name, indexes_for, purge

RUN_ID = uuid.uuid4().hex[:8]
EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
TIMEOUT_S = 120

# scenario -> why H1 differs from the documented value
H1_DISAGREES: dict = {
    "order_rare_types": "H1 ranks NaN with the numbers (documented: before them) and bytes and geo points with maps, "
                        "so an array sorts before them (documented: bytes < reference < geo point < array < vector < map)",
    "update_precondition_deleted": "H1 checks existence first and raises NotFound; the emulator raises FailedPrecondition "
                                   "(unpinned by the documentation: the test database decides)",
    "composite_served_in_reverse": "H1 refuses a query whose directions are all flipped against the declared composite "
                                   "(unpinned: the test database decides whether Firestore scans the index backwards)",
}
# scenario -> why the emulator differs
EMULATOR_DISAGREES: dict = {}


def _marks(table: dict, scenario) -> list:
    reason = table.get(scenario.name)
    return [pytest.mark.xfail(strict=True, reason=reason)] if reason else []


def _params(table: dict, skip_refusals: bool = False) -> list:
    out = []
    for s in SCENARIOS:
        marks = _marks(table, s)
        if skip_refusals and s.refuses:
            marks.append(pytest.mark.skip(reason="the emulator does not refuse a query for a missing index"))
        out.append(pytest.param(s, id=s.name, marks=marks))
    return out


def test_scenarios_are_well_formed():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))
    assert all(s.group for s in SCENARIOS if s.refuses)
    for s in SCENARIOS:
        assert indexes_for(s, collection_name(s, RUN_ID, fixed=True)) or not s.group


@pytest.mark.parametrize("scenario", _params(H1_DISAGREES))
async def test_scenario_on_h1(scenario):
    col = collection_name(scenario, RUN_ID)
    db = EvalDb(indexes=indexes_for(scenario, col))     # the refusal is on; a scenario that needs an index declares it
    with eval_transactions():
        assert await asyncio.wait_for(scenario.run(db, col), TIMEOUT_S) == scenario.expected


@pytest.mark.skipif(not EMULATOR, reason="FIRESTORE_EMULATOR_HOST is not set: start the Firestore emulator (tests/CLAUDE.md)")
@pytest.mark.parametrize("scenario", _params(EMULATOR_DISAGREES, skip_refusals=True))
async def test_scenario_on_emulator(scenario):
    col = collection_name(scenario, RUN_ID)
    db = firestore.AsyncClient(project="p5-parity")     # the emulator host in the environment makes it anonymous
    try:
        assert await asyncio.wait_for(scenario.run(db, col), TIMEOUT_S) == scenario.expected
    finally:
        await purge(db, col)
