"""P11 card E: the GCS parity scenarios (tests/support/gcs_parity.py) on the shared fake. The same scenarios run on the
versioned test bucket in tests/cloud/test_gcs_parity_cloud.py; the scenarios marked `p12` are settled there."""
import pytest

from tests.support.gcs_fake import FakeGcs
from tests.support.gcs_parity import SCENARIOS, purge

BUCKET = "parity-test"


def test_scenarios_are_well_formed():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))
    assert {s.name for s in SCENARIOS if s.p12} == {
        "list_order_of_the_versions_of_one_name", "patch_of_a_noncurrent_version_by_generation",
        "conditional_delete_of_a_missing_object", "name_over_1024_bytes_is_refused"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_on_the_fake(scenario):
    gcs = FakeGcs(buckets={BUCKET: "versioned"})
    client = gcs.client()
    prefix = f"parity/run/{scenario.name}/"
    assert scenario.run(client, BUCKET, prefix) == scenario.expected
    purge(client, BUCKET, prefix)
    assert list(client.list_blobs(BUCKET, prefix=prefix, versions=True)) == []
