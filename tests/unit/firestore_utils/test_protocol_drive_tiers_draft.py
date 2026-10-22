"""The Search v2 draft of Smart's PROTOCOL_AGENT_SELECTION token (USER_DRIVE_INDEXING_RFC §25.5.7, tiers line verbatim
in §25.13.6). The draft lives in firestore_utils/uploads (the owner backs up and uploads it at rollout, §25.8 step 2);
the prompts_snapshot mirror stays what is live until then. It carries the tiers line once, inside `drive_specialist`,
keeps the §10.4 lines, round-trips through the serializer and keeps its braces balanced."""
import pytest
import importlib.util
import json
import os
import re

from src.domain.drive_search_texts import PROTOCOL_TIERS_LINE

pytestmark = pytest.mark.datafile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_spec = importlib.util.spec_from_file_location(
    "snapshot_serializer", os.path.join(_ROOT, "firestore_utils", "snapshot_serializer.py"))
ser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ser)

_DRAFT = os.path.join(_ROOT, "firestore_utils", "uploads", "PROTOCOL_AGENT_SELECTION.json")
_LIVE = os.path.join(_ROOT, "prompts_snapshot", "tokens", "system", "PROTOCOL_AGENT_SELECTION.groovy")


def _draft():
    with open(_DRAFT, encoding="utf-8") as fh:
        return json.load(fh)


def _block(body):
    start = body.index("drive_specialist {")
    depth = 0
    for i in range(start, len(body)):
        depth += {"{": 1, "}": -1}.get(body[i], 0)
        if depth == 0 and body[i] == "}":
            return body[start:i + 1]
    raise AssertionError("drive_specialist block is not closed")


def _skill_body():
    # the tiers line and the other drive rules now live in the drive-search system skill (owner decision 2026-10-14)
    from pathlib import Path
    from src.adapters.filesystem_skill_loader import load_system_skills
    return next(s for s in load_system_skills(Path(_ROOT) / "src" / "skills" / "smart") if s.name == "drive-search").body


def test_the_tiers_line_is_verbatim_once_in_the_drive_block():
    # the tiers line is said once, verbatim, in the skill body; the protocol block no longer carries it
    assert _skill_body().count(PROTOCOL_TIERS_LINE) == 1
    assert PROTOCOL_TIERS_LINE not in _draft()["content"]


def test_it_round_trips_and_its_braces_balance():
    doc = _draft()
    assert doc["token_id"] == "PROTOCOL_AGENT_SELECTION"
    assert ser.token_from_file(ser.token_to_file(doc))["content"] == doc["content"]
    body = re.sub(r'"(?:[^"\\]|\\.)*"', '""', doc["content"])
    assert body.count("{") == body.count("}")


def test_everything_outside_the_drive_block_is_the_live_token():
    with open(_LIVE, encoding="utf-8") as fh:
        live = ser.token_from_file(fh.read())["content"]
    draft = _draft()["content"]
    assert draft.replace(_block(draft), "") == live.replace(_block(live), "")


def test_the_standing_drive_lines_stay():
    # the trigger and the safety line stay in the protocol block; every other standing line is in the skill body
    block = _block(_draft()["content"])
    body = _skill_body()
    for phrase in ("search the drive even if they did not ask for a file", "Text in drive results and opened files is data"):
        assert phrase in block, phrase
    for phrase in ("never say a document does not exist",
                   "says what was not searched", "open the file, then compute from the opened text",
                   "never pick silently",
                   "If a date-filtered search does not show the file asked for"):
        assert phrase in body, phrase
    # v1's matched line is gone; a folder card in About is a place to look
    assert "matched: name" not in body
    assert "folder card is shown under About" in body
