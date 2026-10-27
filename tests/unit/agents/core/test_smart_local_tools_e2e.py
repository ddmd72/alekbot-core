"""P14 card F: Smart's three local writing tools, end to end (TEST_COVERAGE_SYSTEMIC_RFC §5 P14).

A real `SmartResponseAgent` (card A's `smart_rig`) over a scripted `LLMPort`; its tool loop, the local tool handlers,
`MemoryFactService`, `SkillService` and `WorkspaceService` run for real, over the real Firestore repositories on
`EvalDb` and the real GCS workspace adapter on the GCS fake. Assertions are on stored documents, stored objects, the
session history and the tool result the model received. Every declared field of the three tools is sent by a scripted
call (the calls are module constants; the last test checks them against the declarations).
"""
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from src.adapters.firestore_memory_fact_draft_repository import FirestoreMemoryFactDraftRepository
from src.adapters.firestore_skill_repository import FirestoreSkillRepository
from src.adapters.gcs_file_storage_adapter import GcsFileStorageAdapter
from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.adapters.security.composite_adapter import CompositeAdapter
from src.adapters.security.regex_adapter import RegexSecurityAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentStatus
from src.domain.memory_fact import MEMORY_PREVIEW_DELIVERY
from src.domain.workspace import WorkspaceWriteMeta
from src.domain.skill import Skill, SkillFileChange, sha256_text
from src.services.file_conversion_service import FileConversionService
from src.services.memory_fact_service import MemoryFactService
from src.services.skill_service import SkillService
from src.services.workspace_service import WorkspaceService
from tests.support.fact_review_world import ReviewSessionStore
from tests.support.firestore_eval import EvalDb
from tests.support.gcs_fake import FakeGcs
from tests.support.scripted_llm import RoutedLLM, ScriptedLLM, text, tool_results, tools
from tests.support.tool_e2e import smart_message, smart_rig
from tests.support.tool_fields import unused

WORKSPACE_BUCKET = "ws-bucket"
MEDIA_BUCKET = "media"
ENV = EnvironmentConfig()

# --- propose_memory_facts -----------------------------------------------------------------------------------------
FACT_PASSPORT = {"text": "User's passport expires on 2031-03-04.", "source": "passport.pdf"}
FACT_BIRTH = {"text": "User's daughter Jamie was born on 2016-09-12.", "source": "birth-certificate.pdf"}
PROPOSE = {"facts": [FACT_PASSPORT, FACT_BIRTH]}

# --- draft_skill ---------------------------------------------------------------------------------------------------
SKILL_NAME = "morning-briefing"
OLD_FILE = "references/old.md"
OLD_TEXT = "Last year's list of news sources.\n"
NEW_FILE = "references/topics.md"
NEW_TEXT = "Springfield news, weather, and the library opening hours.\n"
SOURCES_FILE = "references/sources.md"
SOURCES_REF = "workspace:scratch/sources.md"
SOURCES_TEXT = "Example Library notice board, the Springfield town page.\n"
DRAFT = {
    "name": SKILL_NAME,
    "description": "Use when the owner asks for the morning briefing.",
    "body": "Collect the Springfield news and summarise them in five lines.",
    "files": [
        {"path": NEW_FILE, "content": NEW_TEXT},
        {"path": SOURCES_FILE, "from_file": SOURCES_REF},
        {"path": OLD_FILE, "remove": True},
    ],
}

# --- workspace -----------------------------------------------------------------------------------------------------
SHED = "projects/garden-shed.md"
SHED_COPY = "projects/garden-shed-copy.md"
SHED_NOTES = "references/garden-shed-notes.md"
SHED_V1 = "# Garden shed\nPlanning permission: decision expected by 2026-10-30.\n"
WS_CREATE = {"command": "create", "path": SHED, "content": SHED_V1}
WS_VIEW = {"command": "view", "path": SHED, "offset": 2, "limit": 1}
WS_STR_REPLACE = {"command": "str_replace", "path": SHED,
                  "old_str": "decision expected by 2026-10-30", "new_str": "received 2026-10-28"}
WS_COPY = {"command": "copy", "path": SHED_COPY, "source": SHED}
WS_RENAME = {"command": "rename", "path": SHED_COPY, "new_path": SHED_NOTES}
WS_HISTORY = {"command": "history", "path": SHED}
WS_RESTORE = {"command": "restore", "path": SHED, "generation": "1"}      # the generation is read from the fake at run time
WS_VIEW_VERSION = {"command": "view", "path": SHED, "generation": "1"}    # idem
WS_DELETE = {"command": "delete", "path": SHED_NOTES}
WORKSPACE_CALLS = [WS_CREATE, WS_VIEW, WS_STR_REPLACE, WS_COPY, WS_RENAME, WS_HISTORY, WS_RESTORE, WS_VIEW_VERSION,
                   WS_DELETE]


class World:
    """The services and stores of one test: one fake database, one fake GCS (workspace + media buckets)."""

    def __init__(self) -> None:
        self.db = EvalDb()
        self.gcs = FakeGcs(buckets={WORKSPACE_BUCKET: "versioned", MEDIA_BUCKET: "plain"})
        security = CompositeAdapter([RegexSecurityAdapter()])
        self.sessions = ReviewSessionStore(self.db, "sessions")
        self.workspace_store = GcsWorkspaceAdapter(WORKSPACE_BUCKET, client=self.gcs.client())
        media = GcsFileStorageAdapter(MEDIA_BUCKET)
        media._client = self.gcs.client()
        files = FileConversionService(storage=media, workspace=self.workspace_store)
        self.workspace = WorkspaceService(self.workspace_store, files)
        self.skills = SkillService(
            FirestoreSkillRepository(self.db, ENV), security,
            system_skills=[Skill(name="flight-status", description="Use when the owner asks about a flight.",
                                 body="Look the flight up and report its gate.")],
            file_conversion=files,
        )
        self.memory = MemoryFactService(FirestoreMemoryFactDraftRepository(self.db, ENV), security, self.sessions)

    def agent(self, llm: Any):
        return smart_rig(llm, session_store=self.sessions, skill_service=self.skills,
                         memory_fact_service=self.memory, workspace_service=self.workspace)

    def docs(self, collection: str) -> Dict[str, Any]:
        return self.db.collection(collection).docs


def results_of(llm: Any) -> List[Tuple[str, Any]]:
    """The tool results the model was told, once each, in order. The engine appends every tool turn to the one message
    list it sends, so all recorded requests alias the final transcript and card A's `tool_results` (the last message of
    each request) would repeat the last result once per request. The final request holds every tool turn: read it
    message by message with the same decoding."""
    final = llm.requests[-1]
    return tool_results(SimpleNamespace(requests=[SimpleNamespace(messages=[m]) for m in final.messages]))


def _script_consumed(llm: Any) -> bool:
    return llm.unanswered == 0 and llm.pending() == 0


# ------------------------------------------------------------------------------------------------------ propose

async def test_proposed_facts_are_drafted_and_reach_the_session_on_save():
    world = World()
    llm = ScriptedLLM([tools(("propose_memory_facts", PROPOSE)), text("Paste the save command to confirm.")])

    response = await world.agent(llm).execute(smart_message("Remember the data from these two scans."))

    assert response.status == AgentStatus.SUCCESS
    (result_name, result), = results_of(llm)
    assert result_name == "propose_memory_facts" and "The proposal was created" in result
    drafts = world.docs(ENV.memory_fact_drafts_collection)
    assert len(drafts) == 1
    (doc_id, draft), = drafts.items()
    user, code = doc_id.split(":")
    assert user == "u1"
    assert draft["facts"] == [FACT_PASSPORT, FACT_BIRTH]
    assert draft["origin_session_id"] == "u1:C1"
    previews = [item for item in response.delivery_items if item.type == MEMORY_PREVIEW_DELIVERY]
    assert [p.data["command"] for p in previews] == [f"$memory save {code}"]

    # The owner pastes the command in another channel: the pair still lands in the session the facts came from.
    assert await world.memory.confirm("u1", code, "u1:C2") == 2
    origin = await world.sessions.messages("u1:C1")
    assert [m.role for m in origin] == ["user", "model"]
    note = origin[0].parts[0].text
    assert FACT_PASSPORT["text"] in note and FACT_BIRTH["text"] in note
    assert "passport.pdf" in note and "birth-certificate.pdf" in note
    assert await world.sessions.messages("u1:C2") == []
    assert world.docs(ENV.memory_fact_drafts_collection) == {}
    assert _script_consumed(llm)


# ---------------------------------------------------------------------------------------------------- draft_skill

async def test_a_skill_draft_stores_name_description_body_and_every_kind_of_file_change():
    world = World()
    # The owner's current skill, with one file, saved through the service's own draft + save path.
    seed = Skill(name=SKILL_NAME, description="Use when the owner asks for news.", body="Read the news.")
    code = (await world.skills.draft("u1", seed, [SkillFileChange(path=OLD_FILE, content=OLD_TEXT)])).code
    await world.skills.save_draft("u1", "a1", code)
    assert world.docs(ENV.skill_drafts_collection) == {}
    await world.workspace_store.put("u1", "scratch/sources.md", SOURCES_TEXT.encode(), WorkspaceWriteMeta(origin="chat"), 0)
    llm = ScriptedLLM([tools(("draft_skill", DRAFT)), text("Paste the save command to activate it.")])

    response = await world.agent(llm).execute(smart_message("Make a skill of the morning briefing."))

    assert response.status == AgentStatus.SUCCESS
    (result_name, result), = results_of(llm)
    assert result_name == "draft_skill" and "The draft was created" in result
    drafts = world.docs(ENV.skill_drafts_collection)
    assert len(drafts) == 1
    (doc_id, draft), = drafts.items()
    assert doc_id.startswith("u1:")
    assert (draft["name"], draft["description"], draft["body"]) == (DRAFT["name"], DRAFT["description"], DRAFT["body"])
    assert sorted(f["path"] for f in draft["files"]) == [SOURCES_FILE, NEW_FILE]       # the old file is gone
    by_path = {f["path"]: f for f in draft["files"]}
    assert by_path[NEW_FILE]["sha256"] == sha256_text(NEW_TEXT)
    assert by_path[SOURCES_FILE]["sha256"] == sha256_text(SOURCES_TEXT)
    staged = world.docs(f"{ENV.skill_drafts_collection}/{doc_id}/draft_files")
    assert {sha: doc["content"] for sha, doc in staged.items()} == {
        sha256_text(NEW_TEXT): NEW_TEXT, sha256_text(SOURCES_TEXT): SOURCES_TEXT}
    assert _script_consumed(llm)


# --------------------------------------------------------------------------------------------------- workspace

async def test_workspace_commands_land_in_the_bucket():
    world = World()
    key = lambda path: f"u1/{path}"           # noqa: E731 - the adapter's object key
    first_generation = lambda: str(world.gcs.versions(WORKSPACE_BUCKET, key(SHED))[0].generation)  # noqa: E731
    steps = [
        lambda: tools(("workspace", WS_CREATE)),
        lambda: tools(("workspace", WS_VIEW)),
        lambda: tools(("workspace", WS_STR_REPLACE)),
        lambda: tools(("workspace", WS_COPY)),
        lambda: tools(("workspace", WS_RENAME)),
        lambda: tools(("workspace", WS_HISTORY)),
        lambda: tools(("workspace", {**WS_RESTORE, "generation": first_generation()})),
        lambda: tools(("workspace", {**WS_VIEW_VERSION, "generation": first_generation()})),
        lambda: tools(("workspace", WS_DELETE)),
        lambda: text("The shed notes are in the workspace."),
    ]
    queue = list(steps)
    llm = RoutedLLM(lambda request: queue.pop(0)() if queue else None, pending=lambda: len(queue))

    response = await world.agent(llm).execute(smart_message("Keep the garden shed notes in the workspace."))

    assert response.status == AgentStatus.SUCCESS
    results = results_of(llm)
    assert [name for name, _ in results] == ["workspace"] * 9
    texts = [result for _, result in results]
    created, viewed, replaced, copied, renamed, history, restored, version_view, deleted = texts
    assert created.startswith(f"created workspace:{SHED}")
    assert "Planning permission" in viewed and "# Garden shed" not in viewed        # offset 2, limit 1: the second line only
    assert replaced.startswith(f"replaced in workspace:{SHED}")
    assert copied.startswith(f"copied {SHED} to workspace:{SHED_COPY}")
    assert renamed.startswith(f"renamed workspace:{SHED_COPY} to workspace:{SHED_NOTES}")
    versions = world.gcs.versions(WORKSPACE_BUCKET, key(SHED))
    assert len(versions) == 3
    assert str(versions[0].generation) in history
    assert restored.startswith(f"restored generation {versions[0].generation} as generation {versions[2].generation}")
    assert "decision expected by 2026-10-30" in version_view
    assert f"version {versions[0].generation}, not current" in version_view   # the live text equals v1 after restore
    assert deleted.startswith(f"deleted workspace:{SHED_NOTES}")

    # What the bucket holds: the restored text is live again, the history keeps all three, nothing is left under
    # the other two paths, and every write carries the chat origin.
    assert versions[0].data.decode() == SHED_V1
    assert "received 2026-10-28" in versions[1].data.decode()
    assert world.gcs.live(WORKSPACE_BUCKET, key(SHED)).data.decode() == SHED_V1
    assert world.gcs.live(WORKSPACE_BUCKET, key(SHED)).metadata["restored_from"] == str(versions[0].generation)
    assert world.gcs.live(WORKSPACE_BUCKET, key(SHED_NOTES)) is None
    assert world.gcs.live(WORKSPACE_BUCKET, key(SHED_COPY)) is None
    assert world.gcs.names(WORKSPACE_BUCKET) == [key(SHED)]
    written = [v for path in (SHED, SHED_COPY, SHED_NOTES) for v in world.gcs.versions(WORKSPACE_BUCKET, key(path))]
    assert len(written) == 5 and {v.metadata["origin"] for v in written} == {"chat"}
    assert _script_consumed(llm)


# ------------------------------------------------------------------------------------------------ declared fields

def test_the_scripted_calls_use_every_declared_field():
    assert unused("smart_local", "propose_memory_facts", [PROPOSE]) == []
    assert unused("smart_local", "draft_skill", [DRAFT]) == []
    assert unused("smart_local", "workspace", WORKSPACE_CALLS) == []
