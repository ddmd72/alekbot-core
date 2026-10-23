"""`main.py` constructs only what is on this list (P9 design §5(e2), R3-S10).

The application graph is built by `build_application_graph`; what `main()` still builds itself is the web layer's
own: voice and MCP. A class constructed in `main()` is a class the graph tests (`wiring_none_ledger.txt`,
`wiring_instance_ledger.txt`) never see, and a second construction of a repository or a store there is the #148 class.
So every call in `main.py` of a CamelCase class that comes from `src.*` must be on `ALLOWED` with a reason, and every
entry must still be constructed there (a stale entry fails).

What counts as a construction: `Name(...)` where the name was imported from a `src` module, at module level or inside
a function, and `mod.Class(...)` or `src.pkg.mod.Class(...)` where `mod` is a `src` module. A classmethod call
(`CloudTaskDelivery.from_headers(...)`) is not a construction.
"""
import ast
import re
from pathlib import Path
from typing import Dict, List

import pytest

MAIN = Path(__file__).resolve().parents[2] / "main.py"
CLASS_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")

ALLOWED: Dict[str, str] = {
    "FirestoreEphemeralStore": "voice: the voice tickets store (env_config.voice_tickets_collection), shared by the voice blueprints and the call setup",
    "VoiceCallSetupService": "voice: persona and ticket preparation for the Cabinet call page, built inside its blueprint call",
    "CloudflareSfuAdapter": "voice: the media room of the web call, built only when the Cloudflare SFU settings and VOICE_RELAY_STREAM_URL are set",
    "SearchEnrichmentService": "MCP: a dedicated instance for the remote server, bypasses the agent stack by design",
    "SearchConfig": "MCP: the limits of that dedicated SearchEnrichmentService",
    "McpErrorLogger": "MCP: wraps the remote server ASGI app to log its errors (_mcp_asgi), nothing else reads it",
}


def _dotted(node: ast.AST) -> str:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _is_class(name: str) -> bool:
    return bool(CLASS_NAME.match(name)) and not name.isupper()


def constructions(source: str) -> Dict[str, List[int]]:
    """`{class name: [line, ...]}` for every construction of a `src` class in `source`."""
    tree = ast.parse(source)
    classes: set = set()      # names imported from a src module (a module alias is lower case, a class is CamelCase)
    modules: set = set()      # names bound to a src module
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (node.module == "src" or node.module.startswith("src.")):
            for alias in node.names:
                bound = alias.asname or alias.name
                (classes if _is_class(alias.name) else modules).add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src."):
                    modules.add(alias.asname or alias.name)
    found: Dict[str, List[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in classes:
            found.setdefault(func.id, []).append(node.lineno)
        elif isinstance(func, ast.Attribute) and _is_class(func.attr):
            owner = _dotted(func.value)
            if owner in modules or owner.startswith("src."):
                found.setdefault(func.attr, []).append(node.lineno)
    return found


def problems(source: str, allowed: Dict[str, str]) -> List[str]:
    built = constructions(source)
    found = [f"main.py:{lines[0]} constructs {name}: put it in the application graph, or list it in ALLOWED with a reason"
             for name, lines in sorted(built.items()) if name not in allowed]
    found += [f"ALLOWED lists {name}, which main.py no longer constructs: remove the entry"
              for name in sorted(allowed) if name not in built]
    found += [f"ALLOWED entry {name} has no reason" for name, reason in sorted(allowed.items()) if len(reason.split()) < 3]
    return found


def test_main_constructs_only_what_is_on_the_list():
    found = problems(MAIN.read_text(), ALLOWED)

    assert not found, "\n".join(found)


# ----------------------------------------------------------------------------- the check, on inline sources

def test_a_module_level_import_that_is_constructed_is_reported():
    source = "from src.adapters.firestore_repo import FirestoreFactRepository\nrepo = FirestoreFactRepository(db)\n"

    assert constructions(source) == {"FirestoreFactRepository": [2]}
    assert "main.py:2 constructs FirestoreFactRepository" in problems(source, {})[0]


def test_a_function_local_import_that_is_constructed_is_reported():
    source = "def main():\n    from src.services.thing import Thing as Renamed\n    return Renamed()\n"

    assert constructions(source) == {"Renamed": [3]}


def test_the_module_dot_class_form_and_the_dotted_form_are_reported():
    source = (
        "from src.adapters import firestore_dedup_store\n"
        "import src.services.thing as thing\n"
        "a = firestore_dedup_store.FirestoreDedupStore(db)\n"
        "b = thing.Thing()\n"
        "c = src.services.other.Other()\n"
    )

    assert set(constructions(source)) == {"FirestoreDedupStore", "Thing", "Other"}


def test_a_classmethod_a_function_a_constant_and_a_foreign_class_are_not_constructions():
    source = (
        "import asyncio\n"
        "from google.cloud import firestore\n"
        "from src.domain.cloud_task_delivery import CloudTaskDelivery\n"
        "from src.utils.logger import logger\n"
        "from src.config.settings import load_settings\n"
        "from src.config.thing import SETTINGS\n"
        "d = CloudTaskDelivery.from_headers(h)\n"
        "logger.info('x')\n"
        "load_settings()\n"
        "SETTINGS()\n"
        "firestore.AsyncClient()\n"
        "asyncio.Event()\n"
    )

    assert constructions(source) == {}


def test_a_stale_entry_and_an_entry_without_a_reason_are_reported():
    source = "from src.services.thing import Thing\nThing()\n"

    found = problems(source, {"Thing": "voice", "Gone": "MCP: a dedicated instance"})

    assert "ALLOWED entry Thing has no reason" in found
    assert any("Gone, which main.py no longer constructs" in p for p in found)


def test_an_allowed_construction_passes():
    source = "from src.services.thing import Thing\nThing()\n"

    assert problems(source, {"Thing": "voice: a dedicated instance"}) == []


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_every_allowed_name_is_a_class_name(name):
    assert _is_class(name)
