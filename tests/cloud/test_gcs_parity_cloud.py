"""P11 card E on the versioned GCS test bucket: the scenarios of tests/support/gcs_parity.py, the arbiter of the shared
fake (tests/support/gcs_fake.py). Run by hand (`make test-cloud`), never in `make check` / `make ci`.

Uses the `gcs_bucket` fixture of tests/cloud/conftest.py (the `cloud` fixture's skips and the test service account's
credentials, plus the bucket guards): the name
comes from `TEST_GCS_BUCKET`, ends in `-test`, is neither `GCS_MEDIA_BUCKET` nor `GCS_WORKSPACE_BUCKET`, and the bucket
is versioned; the client's credentials are those of `TEST_SA_EMAIL`. Without `TEST_GCS_BUCKET` every test skips (the
weekly workflow does not pass it yet: P12). Each scenario writes under `parity/<run id>/<scenario>/` and erases every
version it wrote; the bucket's 1-day lifecycle catches a crashed run.
"""
import uuid

import pytest

from tests.support.gcs_parity import SCENARIOS, purge

RUN_ID = uuid.uuid4().hex[:8]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_on_the_test_bucket(scenario, gcs_bucket):
    client, bucket = gcs_bucket
    prefix = f"parity/{RUN_ID}/{scenario.name}/"
    try:
        result = scenario.run(client, bucket, prefix)
    finally:
        purge(client, bucket, prefix)
    assert result == scenario.expected
