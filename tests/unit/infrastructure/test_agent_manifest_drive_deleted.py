"""`drive_deleted` on search_files_in_drive (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.11, D48; the same flag as
docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4). The delegate tool keeps the first description seen per field name, so the
two features must share one description and no other intent may use the name."""
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent

# Every search_files_in_drive field description names its intent (test_agent_manifest_drive_search.py), the shared one too.
SHARED = ("search_files_in_drive: true only when the user explicitly asks about deleted or removed files: searches "
          "transcripts of deleted recordings and your notes on deleted files. Never for an ordinary search.")


def test_description_says_explicitly():
    assert "explicitly" in SHARED and "Never for an ordinary search" in SHARED
    prose = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]
    assert "only when the user explicitly asks about deleted or removed files" in prose
    assert "Never for an ordinary search" in prose
