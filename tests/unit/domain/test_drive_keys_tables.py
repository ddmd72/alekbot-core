"""The keyer's tables are versioned (USER_DRIVE_INDEXING_RFC §25.2.4, §25.4.8): every table an index key depends on
is hashed, and the hash is pinned per KEYS_VERSION and name-keys version. A change to a table without a version bump
fails here: bump KEYS_VERSION (and NAME_KEYS_V2 when the forms change), then pin the new digest beside the old one."""
from src.domain.drive_index import KEYS_VERSION
from src.domain.drive_keys import NAME_KEYS_V2, index_tables_digest

PINNED = {
    (1, 2): "c54ed0c7d2c1c0a8601c71047dac8663c88bb301f55798373feb7ae7f836d3f4",
}


def test_the_index_tables_are_pinned_to_their_versions():
    digest = index_tables_digest()
    assert PINNED.get((KEYS_VERSION, NAME_KEYS_V2)) == digest, (
        f"keyer tables changed (digest {digest}): bump KEYS_VERSION / NAME_KEYS_V2 and pin the new digest")


def test_the_digest_is_stable_across_calls():
    assert index_tables_digest() == index_tables_digest()
