"""Code-written notes in tool results never claim the system's voice (SYSTEM_TURN_FINGERPRINT_RFC §4.5): no string
literal in the files that build them starts with `SYSTEM:`, `SYSTEM ERROR` or `[System:`. Current-turn and persisted
`[System:` lines live in other files (the RFC's classes (a)/(b)) and are not scanned here."""
import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
_FILES = [
    "domain/document_open_texts.py",
    "domain/email_open_texts.py",
    "services/file_conversion_service.py",
    "infrastructure/delegation_engine.py",
    "infrastructure/skill_tools.py",
    "infrastructure/workspace_tools.py",
    "services/workspace_service.py",
    "infrastructure/memory_fact_tools.py",
]
_FORBIDDEN = ("SYSTEM:", "SYSTEM ERROR", "[System:")


def _literals(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        # f-string pieces are Constant nodes inside a JoinedStr, so a prefix after an f" is caught as well
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def test_the_guarded_files_exist():
    assert [rel for rel in _FILES if not (_SRC / rel).exists()] == [], "a file moved: update this list"


@pytest.mark.parametrize("rel", _FILES)
def test_no_literal_starts_in_the_system_voice(rel):
    bad = [(line, value[:40]) for line, value in _literals(_SRC / rel)
           if value.lstrip().startswith(_FORBIDDEN)]
    assert bad == [], f"{rel} writes a tool-result note in the system's voice: {bad}"
