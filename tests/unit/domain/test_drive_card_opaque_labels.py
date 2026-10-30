"""Opaque-root card labels (USER_DRIVE_INDEXING_RFC §5.2, §8.5): a tool-made folder is "generated files", a
repository is "repository", and a dense folder's item count is a bucket, so its card text does not change with
every child that comes or goes."""
import pytest

from src.domain.drive_card import card_hash, card_text
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState, Scope


def _root(name, reason, child_count=0):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="f", name=name, is_folder=True, kind="folder",
                            scope=Scope.OPAQUE_ROOT, state=RecordState.METADATA_ONLY, reason=reason,
                            child_count=child_count)


@pytest.mark.parametrize("name", ["node_modules", "__pycache__", "site-packages"])
def test_a_generated_directory_is_carded_as_generated_files(name):
    assert card_text(_root(name, Reason.OPAQUE_ROOT, 40_000)) == f"{name} | generated files"


def test_a_repository_is_carded_as_a_repository():
    assert card_text(_root("alekbot", Reason.OPAQUE_ROOT, 40)) == "alekbot | repository"


@pytest.mark.parametrize("count,label", [
    (501, "500+"), (999, "500+"), (1000, "1000+"), (4_999, "1000+"), (5_000, "5000+"), (12_000, "10000+"),
])
def test_a_dense_folder_card_carries_a_bucketed_count(count, label):
    assert card_text(_root("logs", Reason.DENSE_TREE, count)) == f"logs | generated files ({label} items)"


def test_a_dense_folder_card_hash_is_stable_within_a_bucket():
    assert card_hash(_root("logs", Reason.DENSE_TREE, 600)) == card_hash(_root("logs", Reason.DENSE_TREE, 601))
