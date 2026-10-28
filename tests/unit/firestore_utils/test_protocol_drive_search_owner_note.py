"""The owner's note in the drive specialist's search rules (DRIVE_FILE_NOTES_RFC §6.2, §6.4 D21; DRIVE_SPECIALIST_RFC
§9.3 B10, C5; U2 design §4.5, Δ19).

Reviewer re-home (U2-8): these pins were tests/unit/skills/test_drive_search_skill_note_ref.py and
test_drive_search_skill_owner_note.py, on the drive-search skill, which the specialist cut deleted (§5.7). The same
behaviour now lives in the PROTOCOL_DRIVE_SEARCH token (the specialist) and Smart's reading line `R_OWNER_NOTE`.

Reviewer (owner decision 2026-10-28, as in c1ec1eee / a01bdd30): the specialist's tokens are rewritten while they are
tuned on evals, so no test here pins PROTOCOL_DRIVE_SEARCH wording or its block names. Dropped: the `drive_note:` ref
line counted once and placed in the deleted-files entry; the `noted by the owner:` line counted once, placed under
reading the results and equal to Smart's line; the "not part of card accuracy" phrase placed there; the accuracy
paragraph verbatim at the head of its block. Kept: the removed tool's name stays out of the token, and Smart's own
reading lines (code strings, not the token)."""
import os
import re

from firestore_utils import snapshot_serializer as serializer
from src.domain import drive_answer_texts as t

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts_snapshot")
NOTE_LINE = ("A `noted by the owner:` line is what the user told you about the file. It outranks the card's own "
             "description and type, and it is not part of `card accuracy`.")
ACCURACY = ("A described file's result carries `card accuracy: N%`. 100% — everything in this card is confirmed by the "
            "file's own text; you can trust the card fully.")


def _body() -> str:
    with open(os.path.join(ROOT, "tokens", "system", "PROTOCOL_DRIVE_SEARCH.groovy"), encoding="utf-8") as f:
        return re.sub(r"\s+", " ", serializer.token_from_file(f.read())["content"])


def test_the_removed_note_tool_is_not_named_in_the_token():
    assert "annotate_drive_file" not in _body()


def test_smarts_owner_note_reading_line():
    assert t.R_OWNER_NOTE.startswith(NOTE_LINE)


def test_smarts_accuracy_reading_line():
    assert t.R_ACCURACY.startswith(ACCURACY)
