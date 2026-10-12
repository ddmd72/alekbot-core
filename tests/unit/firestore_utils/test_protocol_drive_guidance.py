"""Smart's drive guidance in its protocol token (USER_DRIVE_INDEXING_RFC §10.4, §24.10, D11, D13).

`render_results` keeps per-result facts; the standing guidance lives in PROTOCOL_AGENT_SELECTION (the token Smart's
profile `smart` assigns). The file is the mirror the owner uploads (`snapshot_upload.py --apply`), so it must
round-trip through the serializer, keep its braces balanced, and carry every §10.4 line once."""
import importlib.util
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_spec = importlib.util.spec_from_file_location(
    "snapshot_serializer", os.path.join(_ROOT, "firestore_utils", "snapshot_serializer.py"))
ser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ser)

_TOKEN = os.path.join(_ROOT, "prompts_snapshot", "tokens", "system", "PROTOCOL_AGENT_SELECTION.groovy")
_PROFILE = os.path.join(_ROOT, "prompts_snapshot", "profiles", "smart.yaml")


def _text():
    with open(_TOKEN, encoding="utf-8") as fh:
        return fh.read()


def _drive_block(body):
    start = body.index("drive_specialist {")
    depth = 0
    for i in range(start, len(body)):
        depth += {"{": 1, "}": -1}.get(body[i], 0)
        if depth == 0 and body[i] == "}":
            return body[start:i + 1]
    raise AssertionError("drive_specialist block is not closed")


def test_smarts_profile_assigns_this_token():
    with open(_PROFILE, encoding="utf-8") as fh:
        assert "PROTOCOL_AGENT_SELECTION:" in fh.read()


def test_the_file_round_trips_and_its_braces_balance():
    text = _text()
    doc = ser.token_from_file(text)
    assert doc["token_id"] == "PROTOCOL_AGENT_SELECTION" and ser.token_to_file(doc) == text
    body = re.sub(r'"(?:[^"\\]|\\.)*"', '""', doc["content"])   # braces inside strings do not count
    assert body.count("{") == body.count("}")


# §10.4 protocol lines (and D13) -> a phrase the block must contain
_LINES = {
    "search when the answer may be in the user's documents": "search the drive even if they did not ask for a file",
    "index_as_of / stale: open before quoting": "open the file (it is read live) before quoting it",
    "building: never say it does not exist": "never say a document does not exist",
    "unread / truncated / names only: say what was not searched": "says what was not searched",
    "folder card: search or list within it": "When a folder card matches",
    "sums and totals: open or compute": "use the compute intent",
    "document authority vs memory": "does not override memory about the user's current situation",
    "version families: never pick silently": "never pick silently",
    "D13: date filters on parsed documents use the file date": "they match the file's date",
}


def test_every_protocol_line_is_said_once():
    block = _drive_block(ser.token_from_file(_text())["content"])
    assert {line: block.count(phrase) for line, phrase in _LINES.items()} == {line: 1 for line in _LINES}


def test_the_block_does_not_restate_the_tool_contract():
    """Token discipline: what the tool does and how drive_query is filled live in the manifest."""
    block = _drive_block(ser.token_from_file(_text())["content"])
    assert "drive_query" not in block and "drive_kind" not in block
