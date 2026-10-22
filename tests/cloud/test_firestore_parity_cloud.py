"""P5 on the Firestore test database: the parity scenarios of tests/support/firestore_parity.py, the arbiter of H1.

Run by hand (`make test-cloud`), never in `make check` / `make ci`. Collections are `test_parity_<run id>_<scenario>`;
the scenarios that need indexes run in the fixed collection of their group (`test_parity_idx_<group>`), whose indexes
are created once through the Admin API and kept. Every test deletes what it wrote.
"""
import asyncio
import uuid

import pytest
from google.api_core.exceptions import GoogleAPICallError

from tests.support.firestore_parity import SCENARIOS, collection_name, indexes_for, purge

RUN_ID = uuid.uuid4().hex[:8]
TIMEOUT_S = 180
FRESH_INDEX_ATTEMPTS = 3        # a fresh index can refuse queries for a while after it reports READY (seen once, not understood)
FRESH_INDEX_WAIT_S = 30


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_scenario_on_test_database(scenario, client, ensure_indexes):
    col = collection_name(scenario, RUN_ID, fixed=True)
    fresh = bool(scenario.group) and await asyncio.to_thread(ensure_indexes, col, indexes_for(scenario, col))
    attempts = FRESH_INDEX_ATTEMPTS if fresh else 1
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(FRESH_INDEX_WAIT_S)
        try:
            result = await asyncio.wait_for(scenario.run(client, col), TIMEOUT_S)
        except GoogleAPICallError:
            if attempt == attempts - 1:
                raise
            continue
        finally:
            await purge(client, col)
        if result == scenario.expected:
            break
    assert result == scenario.expected
