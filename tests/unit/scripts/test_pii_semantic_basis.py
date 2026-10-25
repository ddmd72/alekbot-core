"""The semantic gate blocks only on the owner's profile or a real identifier; other findings are notes.

Invented test data that merely describes a family member, a health value or a car is what the repository is made of:
blocking on it made every change red (2026-10-25). The fake API and profile come from the main gate test.
"""

from tests.unit.scripts.test_pii_semantic_check import LEAKY_LINE, api, reply, run  # noqa: F401 (fixture)


def _finding(basis=None, **extra):
    item = {"file": "docs/a.md", "line": 2, "category": "family", "reason": "names a relative"}
    if basis is not None:
        item["basis"] = basis
    item.update(extra)
    return item


def test_a_finding_on_no_basis_is_a_note_and_passes(api, capsys, tmp_path):
    api.script = [(200, reply([_finding("none")]))]
    code, out, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello", "the user's mother Maria Doe"]})
    assert code == 0
    assert "note: docs/a.md:2 family - names a relative" in out.splitlines()
    assert "BLOCKED" not in err and "1 notes" in out


def test_a_profile_match_blocks(api, capsys, tmp_path):
    api.script = [(200, reply([_finding("profile")]))]
    code, out, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello", LEAKY_LINE]})
    assert code == 1 and "docs/a.md:2 family - names a relative" in out.splitlines() and "BLOCKED" in err


def test_a_real_identifier_blocks(api, capsys, tmp_path):
    api.script = [(200, reply([_finding("identifier", category="contact", reason="a real phone number")]))]
    code, out, _ = run(api, capsys, tmp_path, {"docs/a.md": ["hello", "call 0000"]})
    assert code == 1 and "docs/a.md:2 contact - a real phone number" in out.splitlines()


def test_a_missing_or_unknown_basis_blocks(api, capsys, tmp_path):
    api.script = [(200, reply([_finding(), _finding("maybe", line=1)]))]
    code, _, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello", LEAKY_LINE]})
    assert code == 1 and "BLOCKED, 2 findings" in err


def test_notes_and_blocking_findings_are_counted_apart(api, capsys, tmp_path):
    api.script = [(200, reply([_finding("profile"), _finding("none", line=1)]))]
    code, out, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello", LEAKY_LINE]})
    assert code == 1 and "BLOCKED, 1 findings (1 notes)" in err
    assert "note: docs/a.md:1 family - names a relative" in out.splitlines()


def test_the_request_asks_for_a_basis_and_says_invented_data_is_not_a_finding(api, capsys, tmp_path):
    run(api, capsys, tmp_path, {"docs/a.md": ["hello"]})
    body = api.requests[0]["body"]
    item = body["output_config"]["format"]["schema"]["properties"]["findings"]["items"]
    assert "basis" in item["required"] and item["properties"]["basis"]["enum"] == ["profile", "identifier", "none"]
    assert "Test data, examples and fixtures in this repository are invented" in body["system"]
    assert "NOT a finding when it does not match the profile" in body["system"]
