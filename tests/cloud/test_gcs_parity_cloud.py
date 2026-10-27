"""P11 card E on the versioned GCS test bucket: the scenarios of tests/support/gcs_parity.py, the arbiter of the shared
fake (tests/support/gcs_fake.py). Run by hand (`make test-cloud`), never in `make check` / `make ci`.

Reuses the `cloud` fixture (its skips and the test service account's credentials) and adds the bucket guards: the name
comes from `TEST_GCS_BUCKET`, ends in `-test`, is neither `GCS_MEDIA_BUCKET` nor `GCS_WORKSPACE_BUCKET`, and the bucket
is versioned; the client's credentials are those of `TEST_SA_EMAIL`. Without `TEST_GCS_BUCKET` every test skips (the
weekly workflow does not pass it yet: P12). Each scenario writes under `parity/<run id>/<scenario>/` and erases every
version it wrote; the bucket's 1-day lifecycle catches a crashed run.
"""
import uuid

import pytest
from google.cloud import storage

from tests.cloud.conftest import _setting
from tests.support.gcs_parity import SCENARIOS, purge

RUN_ID = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def gcs_bucket(cloud):
    bucket = _setting("TEST_GCS_BUCKET")
    if not bucket:
        pytest.skip("GCS parity not configured: TEST_GCS_BUCKET must be set")
    if not bucket.endswith("-test"):
        pytest.fail("guard: the test bucket name does not end in -test")
    if bucket in {_setting("GCS_MEDIA_BUCKET"), _setting("GCS_WORKSPACE_BUCKET")}:
        pytest.fail("guard: the test bucket is a live bucket")
    client = storage.Client(project=cloud.project, credentials=cloud.credentials)
    if client.project != cloud.project or \
            getattr(client._credentials, "service_account_email", None) != _setting("TEST_SA_EMAIL"):
        pytest.fail("guard: the constructed storage client does not use the test project and service account")
    if not client.get_bucket(bucket).versioning_enabled:
        pytest.fail("guard: the test bucket is not versioned (every scenario assumes versioning)")
    return client, bucket


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_on_the_test_bucket(scenario, gcs_bucket):
    client, bucket = gcs_bucket
    prefix = f"parity/{RUN_ID}/{scenario.name}/"
    try:
        result = scenario.run(client, bucket, prefix)
    finally:
        purge(client, bucket, prefix)
    assert result == scenario.expected
