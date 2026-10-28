"""`drive_deleted` on search_files_in_drive (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.11, D48; the same flag as
docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4). The delegate tool keeps the first description seen per field name, so the
two features must share one description and no other intent may use the name."""

# Every search_files_in_drive field description names its intent (test_agent_manifest_drive_search.py), the shared one too.
SHARED = ("search_files_in_drive: true only when the user explicitly asks about deleted or removed files: searches "
          "transcripts of deleted recordings and your notes on deleted files. Never for an ordinary search.")


def test_description_says_explicitly():
    # Reviewer edit at the cut (DRIVE_SPECIALIST_RFC §5.2: drive_deleted → search_deleted_recordings_and_notes; §8.1):
    # the flag is the specialist's own tool now, and today's rule (SHARED) moves to that tool's description
    from src.domain import drive_tool_texts as tt
    assert "explicitly" in SHARED and "Never for an ordinary search" in SHARED
    prose = next(t for t in tt.drive_tool_declarations() if t["name"] == tt.SEARCH_DELETED)["description"]
    assert "when the user explicitly asks about deleted or removed files" in prose
    assert "Never for an ordinary search" in prose
