"""The cut (DRIVE_SPECIALIST_RFC §2.1.2, §7.6, §10 step 4): the 8 drive intents leave the system in one change. No
module under src/ names one in a string any model reads (or in a docstring: the docs stay in sync), no model-facing
string names a field Smart no longer has, the Intent constants are gone, the drive-search system skill is gone, and no
prompt mirror or upload draft names them. A stale name would cost a rejected call; a rewording of a result string is
in plain words for Smart and in the specialist's parameter names for the specialist."""
import ast
import io
import json
import re
import tokenize
from pathlib import Path

from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent

ROOT = Path(__file__).resolve().parents[2]
REMOVED_INTENTS = ("search_files_in_drive", "save_file_to_drive", "list_files_in_drive", "open_file_from_drive",
                   "move_file_in_drive", "create_folder_in_drive", "update_file_in_drive", "delete_file_from_drive")
SMART_ERA_FIELDS = ("drive_query", "drive_folder", "drive_kind", "drive_date_from", "drive_date_to", "drive_deleted")
_FIELD = re.compile(r"(?<!\w)(" + "|".join(SMART_ERA_FIELDS) + r")(?!\w)")   # whole names: not drive_deleted_file
REMOVED_CONSTANTS = ("SEARCH_FILES_IN_DRIVE", "SAVE_FILE_TO_DRIVE", "LIST_FILES_IN_DRIVE", "OPEN_FILE_FROM_DRIVE",
                     "MOVE_FILE_IN_DRIVE", "CREATE_FOLDER_IN_DRIVE", "UPDATE_FILE_IN_DRIVE", "DELETE_FILE_FROM_DRIVE")


def _docstring_lines(source: str) -> set:
    lines: set = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                lines.add(first.lineno)
    return lines


def _strings(path: Path):
    source = path.read_text(encoding="utf-8")
    docs = _docstring_lines(source)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.STRING or getattr(tokenize, "FSTRING_MIDDLE", None) == tok.type:
            yield tok.start[0], tok.string, tok.start[0] in docs


def _src_files():
    return sorted((ROOT / "src").rglob("*.py"))


def test_no_src_string_or_docstring_names_a_removed_intent():
    found = [f"{p.relative_to(ROOT)}:{line}" for p in _src_files() for line, text, _ in _strings(p)
             if any(name in text for name in REMOVED_INTENTS)]
    assert not found, found


def test_no_model_facing_src_string_names_a_smart_era_drive_field():
    """Docstrings may name a Python parameter (`query_concepts(drive_query)`); strings a model reads may not."""
    found = [f"{p.relative_to(ROOT)}:{line}" for p in _src_files() for line, text, is_doc in _strings(p)
             if not is_doc and _FIELD.search(text)]
    assert not found, found


def test_the_intent_constants_are_gone_and_no_descriptor_offers_the_old_intents():
    assert not [name for name in REMOVED_CONSTANTS if hasattr(Intent, name)]
    offered = {intent for d in ALL_DESCRIPTORS for intent in d.capabilities}
    assert not offered & set(REMOVED_INTENTS)


def test_the_drive_search_system_skill_is_gone():
    assert not (ROOT / "src" / "skills" / "smart" / "drive-search").exists()


def test_no_prompt_mirror_or_upload_draft_names_a_removed_intent_or_the_skill():
    texts = {p: p.read_text(encoding="utf-8") for p in (ROOT / "prompts_snapshot").rglob("*") if p.is_file()}
    for p in (ROOT / "firestore_utils" / "uploads").glob("*.json"):
        texts[p] = json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False)
    found = [str(p.relative_to(ROOT)) for p, text in texts.items()
             if any(name in text for name in REMOVED_INTENTS + ("drive-search",))]
    assert not found, found


async def test_the_coordinator_answers_every_removed_intent_as_unknown():
    """Review M2: a stale text's call to a removed intent gets the coordinator's unknown-intent refusal, which Smart
    self-corrects from (the residual of the decision record), not a crash or a silent success."""
    from src.domain.agent import AgentStatus
    from src.infrastructure.agent_coordinator import AgentCoordinator
    from src.infrastructure.agent_registry import AgentRegistry

    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = AgentCoordinator(registry=registry)
    for intent in REMOVED_INTENTS:
        response = await coordinator.handle_delegation(intent, "keep it", {"user_id": "u1"}, "smart_response_agent_u1")
        assert response.status == AgentStatus.FAILED, intent
        assert response.error == f"No agent registered for intent: {intent}"
        assert intent not in (response.suggestions or [])
