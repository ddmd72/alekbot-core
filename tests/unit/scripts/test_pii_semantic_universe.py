"""The semantic gate takes the invented universe from a private file named by PII_UNIVERSE_FILE.

The public code lists no cast: with the variable set, the file's text is the allowed fiction in the prompt; unset, a
short generic sentence stands in; set but unreadable or empty, the check fails closed. The universe text here is
invented for the PII-gate self-tests. The fake API and profile come from the main gate test.
"""

from tests.unit.scripts.test_pii_semantic_check import api, run  # noqa: F401 (fixture)

from scripts.pii_gate import semantic_check

UNIVERSE_TEXT = "Invented universe for the gate self-test: cousin Zorblax, who keeps bees in a town called Nowhereton."


def _system(api) -> str:
    return api.requests[0]["body"]["system"]


def test_the_universe_file_text_is_embedded_as_the_allowed_fiction(api, capsys, tmp_path, monkeypatch):
    universe = tmp_path / "universe.md"
    universe.write_text(UNIVERSE_TEXT + "\n", encoding="utf-8")
    monkeypatch.setenv("PII_UNIVERSE_FILE", str(universe))
    code, _, _ = run(api, capsys, tmp_path, {"docs/a.md": ["hello"]})
    assert code == 0
    system = _system(api)
    assert f"<invented_universe>\n{UNIVERSE_TEXT}\n</invented_universe>" in system
    assert semantic_check.GENERIC_UNIVERSE not in system


def test_every_chunk_carries_the_universe(api, capsys, tmp_path, monkeypatch):
    universe = tmp_path / "universe.md"
    universe.write_text(UNIVERSE_TEXT, encoding="utf-8")
    monkeypatch.setenv("PII_UNIVERSE_FILE", str(universe))
    monkeypatch.setattr(semantic_check, "CHUNK_BYTES", 200)
    code, _, _ = run(api, capsys, tmp_path, {"docs/a.md": [f"line {i} " + "x" * 60 for i in range(8)]})
    assert code == 0
    assert len(api.requests) > 1
    assert all(UNIVERSE_TEXT in r["body"]["system"] for r in api.requests)


def test_unset_variable_uses_a_generic_sentence_with_no_names(api, capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("PII_UNIVERSE_FILE", raising=False)
    code, _, _ = run(api, capsys, tmp_path, {"docs/a.md": ["hello"]})
    assert code == 0
    system = _system(api)
    assert f"<invented_universe>\n{semantic_check.GENERIC_UNIVERSE}\n</invented_universe>" in system


def test_a_blank_variable_counts_as_unset(api, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("PII_UNIVERSE_FILE", "   ")
    code, _, _ = run(api, capsys, tmp_path, {"docs/a.md": ["hello"]})
    assert code == 0
    assert semantic_check.GENERIC_UNIVERSE in _system(api)


def test_the_public_code_lists_no_cast():
    assert not hasattr(semantic_check, "_INVENTED_CAST")
    generic = semantic_check.GENERIC_UNIVERSE
    for name in ("Alex", "Doe", "Springfield", "Oak Street", "Skoda", "555", "example.com"):
        assert name not in generic


def test_a_missing_universe_file_fails_closed_before_any_call(api, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("PII_UNIVERSE_FILE", str(tmp_path / "no-such-file.md"))
    code, out, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello"]})
    assert code == 2
    assert api.requests == []
    assert "PII_UNIVERSE_FILE" in err and "failing closed" in err
    assert "no-such-file" not in out + err


def test_an_empty_universe_file_fails_closed(api, capsys, tmp_path, monkeypatch):
    universe = tmp_path / "universe.md"
    universe.write_text("\n  \n", encoding="utf-8")
    monkeypatch.setenv("PII_UNIVERSE_FILE", str(universe))
    code, _, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello"]})
    assert code == 2 and api.requests == [] and "empty" in err


def test_the_universe_text_is_never_printed(api, capsys, tmp_path, monkeypatch):
    universe = tmp_path / "universe.md"
    universe.write_text(UNIVERSE_TEXT, encoding="utf-8")
    monkeypatch.setenv("PII_UNIVERSE_FILE", str(universe))
    api.script = [(200, {"type": "message", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": '{"findings": [{"file": "docs/a.md", "line": 1, '
                                      '"category": "family", "basis": "profile", "reason": "names a relative"}]}'}]})]
    code, out, err = run(api, capsys, tmp_path, {"docs/a.md": ["hello"]})
    assert code == 1
    assert "Zorblax" not in out + err and "Nowhereton" not in out + err


def test_nothing_to_read_passes_without_touching_the_universe_file(api, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("PII_UNIVERSE_FILE", str(tmp_path / "no-such-file.md"))
    code, out, _ = run(api, capsys, tmp_path, {"docs/a.md": ["   "]})
    assert code == 0 and "no added lines" in out and api.requests == []
