"""OS and lock artefacts are kept as records but never searchable (USER_DRIVE_INDEXING_RFC §3.4, §5.1): no card and
no name keys, so an Office lock file never ranks above the document it locks."""
import pytest

from src.domain.drive_card import card_eligible
from src.domain.drive_index import DriveChange, Reason, RecordState, Scope
from src.domain.drive_index_policy import item_rule

from .test_drive_index_planner import ch, observe


@pytest.mark.parametrize("name", ["~$Договор_ред3.docx", "Thumbs.db", "desktop.ini", ".DS_Store", "build.tmp"])
def test_os_artefacts_are_kept_without_card_or_name_keys(name):
    assert item_rule(DriveChange(item_id="i", name=name)) is Reason.OS_ARTIFACT
    rec = observe(ch("a", name)).upsert
    assert (rec.scope, rec.state, rec.reason) == (Scope.NORMAL, RecordState.METADATA_ONLY, Reason.OS_ARTIFACT)
    assert not card_eligible(rec) and rec.name_tokens == () and rec.due_at is None


def test_other_dot_names_keep_their_card():
    rec = observe(ch("a", ".gitignore")).upsert
    assert rec.reason is Reason.DOT_NAME and card_eligible(rec) and rec.name_tokens
