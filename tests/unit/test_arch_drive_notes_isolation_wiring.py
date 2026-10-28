"""The drive-note store is never HANDED to the memory side (DRIVE_FILE_NOTES_RFC §1.4 anti-goal; the import side is
tests/unit/test_arch_drive_notes_isolation.py): wherever the composition roots construct ConsolidationService,
SearchEnrichmentService, BiographicalContextService or the remote MCP server's components, no argument names a drive
note, and none of their constructors has a parameter named or typed for one. Not every path is closed by structure:
ConsolidationService takes `agent_factory` and memory-side code can reach a `coordinator`, and both can lead to the
note service. What blocks that is that memory-side code never names it — pinned case-insensitively below (named
residual, plan part A §6)."""
import ast
import inspect
from pathlib import Path

import pytest

from src.composition.mcp_setup import build_mcp_components
from src.services.biographical_context_service import BiographicalContextService
from src.services.consolidation_service import ConsolidationService
from src.services.search_enrichment_service import SearchEnrichmentService

_ROOT = Path(__file__).resolve().parents[2]
_ROOTS = ["main.py", "src/composition/application_graph.py", "src/composition/service_container.py",
          "src/composition/user_agent_factory.py", "src/composition/mcp_setup.py"]
_MEMORY_SIDE_FILES = ["src/services/consolidation_service.py",
                      "src/agents/consolidation_agent.py", "src/services/biographical_context_service.py",
                      "src/services/search_enrichment_service.py",
                      *sorted(str(p.relative_to(_ROOT)) for p in (_ROOT / "src" / "composition").glob("mcp_*.py"))]
_MEMORY_SIDE = ("ConsolidationService", "SearchEnrichmentService", "BiographicalContextService",
                "build_mcp_components")


def _callee(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _names(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Attribute):
            yield sub.attr


def test_every_composition_root_exists():
    assert [r for r in _ROOTS if not (_ROOT / r).exists()] == []


@pytest.mark.parametrize("rel", _ROOTS)
def test_no_memory_side_construction_receives_a_drive_note(rel):
    tree = ast.parse((_ROOT / rel).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _callee(n) in _MEMORY_SIDE]
    for call in calls:
        keywords = [k.arg or "" for k in call.keywords]
        values = [name for part in [*call.args, *(k.value for k in call.keywords)] for name in _names(part)]
        leaks = [n for n in keywords + values if "drive_note" in n.lower()]
        assert leaks == [], f"{rel}:{call.lineno} hands {_callee(call)} a drive note: {leaks}"


@pytest.mark.parametrize("target", [ConsolidationService, SearchEnrichmentService, BiographicalContextService,
                                    build_mcp_components])
def test_no_memory_side_constructor_takes_a_drive_note(target):
    signature = inspect.signature(target)
    for name, param in signature.parameters.items():
        text = f"{name} {param.annotation}".lower()
        assert "drive_note" not in text and "drivenote" not in text, f"{target.__name__}({name})"


@pytest.mark.parametrize("rel", _MEMORY_SIDE_FILES)
def test_no_memory_side_module_names_a_drive_note_in_any_case(rel):
    text = (_ROOT / rel).read_text().lower()
    assert "drive_note" not in text and "drivenote" not in text, rel
