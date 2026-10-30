"""T-1, the slice-1b core part (MULTI_DRIVE_RFC §13): the modules slice 1b adds to domain and ports hold no string
constant (docstrings included) that names a provider or a provider shape. The full T-1 over every layer lands when the
single-drive constants leave `src/domain` (the parts of slice 1b); the registry and adapters are exempt by the RFC."""
import ast
import re
from pathlib import Path

import pytest

_PROVIDER_RE = re.compile(r"(?<![a-z])(onedrive|graph|microsoft|google|gdrive|approot|quickxor|ctag|takendatetime)",
                          re.IGNORECASE)

ROOT = Path(__file__).resolve().parents[3]
C1B_MODULES = (
    "src/domain/connection_scope.py",
    "src/domain/drive_capabilities.py",
    "src/domain/drive_connection.py",
    "src/domain/drive_connection_state.py",
    "src/domain/drive_connection_status.py",
    "src/domain/drive_content_identity.py",
    "src/domain/drive_handle.py",
    "src/domain/drive_media_facts.py",
    "src/domain/drive_media_source.py",
    "src/domain/drive_ref.py",
    "src/domain/drive_untagged_ref.py",
    "src/domain/drive_user_settings.py",
    "src/ports/drive_connection_repository.py",
    "src/ports/user_drive_port.py",
)


def _hits(source: str):
    return [(node.lineno, node.value) for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _PROVIDER_RE.search(node.value)]


@pytest.mark.parametrize("module", C1B_MODULES)
def test_no_provider_string_constant(module):
    assert _hits((ROOT / module).read_text()) == []


def test_the_scan_catches_a_provider_name_but_not_an_ordinary_word():
    assert [v for _, v in _hits('X = "the cTag field"\n"""a quickXorHash"""\nY = "Graph API"')] == [
        "the cTag field", "a quickXorHash", "Graph API"]
    assert _hits('"""a photograph, a paragraph"""') == []
