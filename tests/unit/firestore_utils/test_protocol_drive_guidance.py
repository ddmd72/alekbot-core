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


def _reading_lines():
    """Smart's half: every reading line a drive answer can carry (src/domain/drive_answer_texts.py)."""
    from src.domain import drive_answer_texts as t
    return "\n".join(v for k, v in vars(t).items() if k.startswith("R_") and isinstance(v, str))


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
    # Search v2 P6 (RFC §25.5.6): the folder rule keys on the About tier, the shortened rule on a cut window; new tiers line
    "folder card: search or list within it": "When a folder card is shown under About",
    "tiers: how each file matched": "Drive results say how each file matched.",
    "sums and totals: open, then compute (review E-M2)": "open the file, then compute from the opened text",
    "document authority vs memory": "does not override memory about the user's current situation",
    "version families: never pick silently": "never pick silently",
    # review E-M3: which date goes where is one distinction in the manifest; the token keeps only the retry rule.
    # Final review F5-4: the retry fires when the file asked for is not shown, not only on zero results
    "dates: a date-filtered search that misses the file is retried without the filter":
        "If a date-filtered search does not show the file asked for, search again without the filter.",
    "shortened passage: open before quoting (review E minor)": "Before quoting more than it shows, open the file.",
}


# Reviewer edit at the cut: each line's home is the half of the former skill that acts on it (DRIVE_SPECIALIST_RFC
# §5.7): the copies in Smart's reading lines, once each where Smart acts on the rule, none where only the specialist does.
# Reviewer (owner decision 2026-10-28): the specialist's token half is no longer counted; its wording is tuned on evals.
_HOMES = {
    "index_as_of / stale: open before quoting": 0,                     # freshness: the specialist opens
    "building: never say it does not exist": 1,
    "unread / truncated / names only: say what was not searched": 1,
    "folder card: search or list within it": 1,
    "tiers: how each file matched": 1,
    "sums and totals: open, then compute (review E-M2)": 1,
    "document authority vs memory": 1,
    "version families: never pick silently": 1,
    "dates: a date-filtered search that misses the file is retried without the filter": 0,
    "shortened passage: open before quoting (review E minor)": 1,
}


def test_every_protocol_line_is_said_once():
    # only the search trigger stays in the protocol block; every other line is said once in Smart's reading lines
    # where Smart acts on it
    block = _drive_block(ser.token_from_file(_text())["content"])
    trigger = "search when the answer may be in the user's documents"
    assert block.count(_LINES[trigger]) == 1
    assert set(_HOMES) == set(_LINES) - {trigger}
    reading = _reading_lines()
    assert {line: reading.count(_LINES[line]) for line in _HOMES} == _HOMES


def test_the_block_does_not_restate_the_tool_contract():
    """Token discipline: what the tool does and how drive_query is filled live in the manifest."""
    block = _drive_block(ser.token_from_file(_text())["content"])
    assert "drive_query" not in block and "drive_kind" not in block
