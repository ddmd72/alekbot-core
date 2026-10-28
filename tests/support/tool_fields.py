"""The one definition of what P14 enumerates: every declared field of every tool a model can call to write something
(TEST_COVERAGE_SYSTEMIC_RFC §5 P14, card A).

A surface is a list of tool declarations, built by the production builder each time it is read:
- `consolidation`, `consolidation_review`: `ConsolidationAgent._get_tool_declarations` for the two pass kinds;
- `notes`, `tasks`: the agents' `_TOOL_DECLARATIONS`;
- `smart_local`: Smart's three local writing tools (`propose_memory_facts`, `draft_skill`, `workspace`);
- `drive`: the drive specialist's twelve tools and its terminal tool (`drive_tool_declarations`, DRIVE_SPECIALIST_RFC
  §8.1);
- `delegate`: the `context` schema Smart's `delegate_to_specialist` declares for one intent, built through the real
  `BaseAgent._build_delegate_tool_declaration`, for `work_with_drive_files` (its one field `file_contents`, the text a
  model writes into a file through the specialist), `manage_self_reminders` and `manage_user_tasks` (wrapped as
  `{"name": <intent>, "parameters": <that context schema>}`).

A field path is `<surface>:<tool>.<path>`: an object with `properties` recurses (`updates.expiry.kind`); an array of
objects has no path of its own, its items' properties become `name[].child` (`files[].path`); anything else is a leaf
(an object without properties such as `metadata`, an array of scalars such as `tags`).

`READ_ONLY` names whole reading tools and `READ_ONLY_FIELDS` the read-only fields of a writing tool: they need no
store test. A test file of the package ends with `unused(surface, tool, calls) == []` for each tool of its surface.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Set

from src.agents import notes_agent, tasks_agent
from src.agents.base_agent import BaseAgent
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.drive_tool_texts import drive_tool_declarations
from src.infrastructure.agent_manifest import DRIVE_SPECIALIST, NOTES, TASKS, AgentDescriptor, Intent
from src.infrastructure.memory_fact_tools import build_propose_memory_facts_tool_declaration
from src.infrastructure.skill_tools import build_draft_skill_tool_declaration
from src.infrastructure.workspace_tools import build_workspace_tool_declaration

# The intents whose `context` fields reach a store.
DELEGATE_INTENTS: Dict[str, AgentDescriptor] = {
    Intent.WORK_WITH_DRIVE_FILES: DRIVE_SPECIALIST,
    Intent.MANAGE_SELF_REMINDERS: NOTES,
    Intent.MANAGE_USER_TASKS: TASKS,
}


def _delegate() -> List[Dict[str, Any]]:
    out = []
    for intent, descriptor in DELEGATE_INTENTS.items():
        declaration = BaseAgent._build_delegate_tool_declaration(
            [{"name": intent, "description": "", "context_schema": descriptor.context_schemas[intent]}])
        out.append({"name": intent, "parameters": declaration["parameters"]["properties"]["context"]})
    return out


SURFACES: Dict[str, Callable[[], List[Dict[str, Any]]]] = {
    "consolidation": lambda: ConsolidationAgent._get_tool_declarations(None, "consolidation"),  # type: ignore[arg-type]
    "consolidation_review": lambda: ConsolidationAgent._get_tool_declarations(None, "review"),  # type: ignore[arg-type]
    "notes": lambda: copy.deepcopy(notes_agent._TOOL_DECLARATIONS),
    "tasks": lambda: copy.deepcopy(tasks_agent._TOOL_DECLARATIONS),
    "smart_local": lambda: [build_propose_memory_facts_tool_declaration(), build_draft_skill_tool_declaration(),
                            build_workspace_tool_declaration()],
    "drive": drive_tool_declarations,
    "delegate": _delegate,
}
IN_SCOPE = tuple(SURFACES)

# The drive specialist's terminal tool (`drive:report_to_orchestrator`) is not here: it writes no store, but its fields
# become the answer the orchestrator's model reads, so each is a `prompt-input` line with a test.
READ_ONLY: FrozenSet[str] = frozenset({
    "consolidation:search_existing_facts", "consolidation:count_words", "consolidation_review:count_words",
    "tasks:list_tasks", "tasks:search_tasks",
    "drive:search_drive", "drive:search_deleted_recordings_and_notes", "drive:list_folder", "drive:open_drive_file",
    "drive:get_recording_link",
})
READ_ONLY_FIELDS: FrozenSet[str] = frozenset({"smart_local:workspace.offset", "smart_local:workspace.limit"})


def flatten(schema: Mapping[str, Any], prefix: str = "") -> List[str]:
    """The field paths a JSON schema declares (see the module docstring for the rules)."""
    out: List[str] = []
    for name, spec in (schema.get("properties") or {}).items():
        path = f"{prefix}{name}"
        if spec.get("type") == "object" and spec.get("properties"):
            out.extend(flatten(spec, f"{path}."))
        elif spec.get("type") == "array" and (spec.get("items") or {}).get("properties"):
            out.extend(flatten(spec["items"], f"{path}[]."))
        else:
            out.append(path)
    return out


def _tool(surface: str, tool: str) -> Dict[str, Any]:
    for declaration in SURFACES[surface]():
        if declaration["name"] == tool:
            return declaration
    raise KeyError(f"{surface} declares no tool {tool!r}")


def declared() -> List[str]:
    """Every declared field of every tool of every surface, as sorted `<surface>:<tool>.<path>` keys."""
    return sorted(f"{surface}:{d['name']}.{path}"
                  for surface, build in SURFACES.items() for d in build()
                  for path in flatten(d.get("parameters") or {}))


def tool_of(key: str) -> str:
    """`"<surface>:<tool>"` of a `declared()` key."""
    return key.split(".", 1)[0]


def used(args: Any, prefix: str = "") -> Set[str]:
    """Every key path of `args` at every depth, prefixes included. Lists are descended only when their elements are
    dicts (`files[].path`); a list of scalars contributes only its own key."""
    out: Set[str] = set()
    if not isinstance(args, Mapping):
        return out
    for key, value in args.items():
        path = f"{prefix}{key}"
        out.add(path)
        if isinstance(value, Mapping):
            out |= used(value, f"{path}.")
        elif isinstance(value, list) and value and all(isinstance(v, Mapping) for v in value):
            for item in value:
                out |= used(item, f"{path}[].")
    return out


def unused(surface: str, tool: str, calls: Iterable[Mapping[str, Any]]) -> List[str]:
    """The declared paths of that tool that no call's `used()` contains, sorted."""
    seen: Set[str] = set()
    for call in calls:
        seen |= used(call)
    return sorted(p for p in flatten(_tool(surface, tool).get("parameters") or {}) if p not in seen)
