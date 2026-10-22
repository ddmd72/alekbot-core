"""P5: the parity scenarios (tests/support/firestore_parity.py) on H1 always, and on the Firestore emulator when
FIRESTORE_EMULATOR_HOST is set. The same scenarios run on the test database in tests/cloud/.

Every expected value was confirmed on the test database on 2026-10-22. H1 differed on two scenarios (the order of NaN,
bytes and geo points; a precondition on a missing document), fixed in firestore_eval.py in the same round.
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


def _params(skip_refusals: bool = False) -> list:
    return [pytest.param(s, id=s.name, marks=[pytest.mark.skip(reason="the emulator does not refuse a query for a missing index")]
                         if skip_refusals and s.refuses else []) for s in SCENARIOS]


def test_scenarios_are_well_formed():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))
    assert all(s.group for s in SCENARIOS if s.refuses)
    for s in SCENARIOS:
        assert indexes_for(s, collection_name(s, RUN_ID, fixed=True)) or not s.group


@pytest.mark.parametrize("scenario", _params())
async def test_scenario_on_h1(scenario):
    col = collection_name(scenario, RUN_ID)
    db = EvalDb(indexes=indexes_for(scenario, col))     # the refusal is on; a scenario that needs an index declares it
    with eval_transactions():
        assert await asyncio.wait_for(scenario.run(db, col), TIMEOUT_S) == scenario.expected


@pytest.mark.skipif(not EMULATOR, reason="FIRESTORE_EMULATOR_HOST is not set: start the Firestore emulator (tests/CLAUDE.md)")
@pytest.mark.parametrize("scenario", _params(skip_refusals=True))
async def test_scenario_on_emulator(scenario):
    col = collection_name(scenario, RUN_ID)
    db = firestore.AsyncClient(project="p5-parity")     # the emulator host in the environment makes it anonymous
    try:
        assert await asyncio.wait_for(scenario.run(db, col), TIMEOUT_S) == scenario.expected
    finally:
        await purge(db, col)
