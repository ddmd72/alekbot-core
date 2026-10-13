"""The keyer is versioned (USER_DRIVE_INDEXING_RFC §25.2.4, §25.4.8). Two pins per KEYS_VERSION: a hash of every table
an index key depends on, and a hash of the keyer's output on a fixed multilingual fixture (P1 review m9: a change to
a rule's code, not only to a table). Either failing means: bump KEYS_VERSION (and NAME_KEYS_V2 when name keys change),
then pin the new digests beside the old ones."""
from src.domain.drive_index import KEYS_VERSION
from src.domain.drive_keys import NAME_KEYS_V2, golden_digest, index_tables_digest

TABLES = {
    (1, 2): "c54ed0c7d2c1c0a8601c71047dac8663c88bb301f55798373feb7ae7f836d3f4",
    (2, 2): "35c2d15703fcf1875fbf7f065fe9a735cd3d2696484d90a9bad048835a8e1140",
}
GOLDEN = {
    (2, 2): "eaddca63bfa2498893cb5644b06e065234c73eab151ec80eace891e3a04a0af5",
}


def test_the_index_tables_are_pinned_to_their_versions():
    digest = index_tables_digest()
    assert TABLES.get((KEYS_VERSION, NAME_KEYS_V2)) == digest, (
        f"keyer tables changed (digest {digest}): bump KEYS_VERSION / NAME_KEYS_V2 and pin the new digest")


def test_the_keyer_output_is_pinned_to_its_versions():
    digest = golden_digest()
    assert GOLDEN.get((KEYS_VERSION, NAME_KEYS_V2)) == digest, (
        f"keyer output changed (digest {digest}): bump KEYS_VERSION / NAME_KEYS_V2 and pin the new digest")


def test_the_digests_are_stable_across_calls():
    assert index_tables_digest() == index_tables_digest() and golden_digest() == golden_digest()
