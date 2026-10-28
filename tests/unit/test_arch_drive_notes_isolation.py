"""The drive-note store never reaches memory (DRIVE_FILE_NOTES_RFC §1.4 anti-goal, D2): the consolidation path, the
biographical cache, SearchEnrichmentService and the remote MCP server's composition import neither the note repository
nor the note service, and no adapter of memory imports the note store. Structural: none of them is given the store."""
import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
_FORBIDDEN = ("drive_note_repository", "drive_note_service", "firestore_drive_note_repo", "in_memory_drive_note_repo",
              "DriveNoteRepository", "DriveNoteService")
_MEMORY_SIDE = [
    "services/consolidation_service.py",
    "agents/consolidation_agent.py",
    "services/biographical_context_service.py",
    "services/search_enrichment_service.py",
    *sorted(str(p.relative_to(_SRC)) for p in (_SRC / "composition").glob("mcp_*.py")),
]


def _imported_names(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""
            for alias in node.names:
                yield alias.name


def test_the_memory_side_files_exist():
    missing = [rel for rel in _MEMORY_SIDE if not (_SRC / rel).exists()]
    assert missing == [], "a memory-side file moved: update this list, never drop the guard"


@pytest.mark.parametrize("rel", _MEMORY_SIDE)
def test_no_memory_side_module_imports_the_note_store(rel):
    names = list(_imported_names(_SRC / rel))
    leaks = [n for n in names if any(f in n for f in _FORBIDDEN)]
    assert leaks == [], f"{rel} imports the drive-note store: {leaks}"


def test_no_memory_side_module_names_the_note_store():
    for rel in _MEMORY_SIDE:
        text = (_SRC / rel).read_text()
        assert "drive_notes_collection" not in text and "DriveNote" not in text, rel
