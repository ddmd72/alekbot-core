"""The drive-search skill's `_Transcripts` clause and shared "Deleted files" line, and the capabilities line
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.11)."""
from pathlib import Path

from src.utils.capabilities import CAPABILITIES_TEXT

# Retargeted at the cut: the skill's recordings and deleted-files rules are the specialist's PROTOCOL_DRIVE_SEARCH token
# now (DRIVE_SPECIALIST_RFC §5.7: "Recordings, _Transcripts, deleted files — all" to the specialist).
SKILL = (Path(__file__).resolve().parents[3] / "prompts_snapshot" / "tokens" / "system"
         / "PROTOCOL_DRIVE_SEARCH.groovy").read_text(encoding="utf-8").split("\n---\n", 1)[1]


def test_skill_names_the_transcripts_folder_as_bot_managed():
    assert "_Transcripts" in SKILL and "never write to it" in SKILL   # the token drops markdown emphasis


def test_skill_deleted_files_line_is_explicit_only():
    assert "Search deleted files (`drive_deleted`) only when the user asks about files that were deleted or removed" in SKILL
    assert "transcripts of deleted recordings and your notes on deleted files" in SKILL


def test_capabilities_line():
    assert "search in deleted files" in CAPABILITIES_TEXT and "_Transcripts" in CAPABILITIES_TEXT
