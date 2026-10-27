"""P14 card G: the five mutating drive intents, every declared context field, end to end (TEST_COVERAGE_SYSTEMIC_RFC §5 P14).

Smart's scripted `delegate_to_specialist(intent, query, context)` goes through the real `DelegationEngine` and a real
`AgentCoordinator` (registry with `FILE_MANAGEMENT`) into the real `FileManagementAgent` and `UserDriveService`, over a
drive fake with Graph's write semantics (`ScriptedDrive`, given the two read methods `UserDriveService` also uses) and
the chat-file store on the GCS fake. Assertions are on the drive's tree and bytes, on the chat texts the receipts
reached, and on the tool result the model was told. Every declared field of the five intents is sent by a scripted
call (module constants; the last test checks them against the declarations).
"""
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.gcs_file_storage_adapter import GcsFileStorageAdapter
from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentStatus
from src.domain.user_drive import DriveItem, DriveItemNotFoundError
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from src.services.user_notification_service import UserNotificationService
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import FakeLanguageService, FakeNotificationState, RecordingChannelFactory
from tests.support.gcs_fake import FakeGcs
from tests.support.scripted_llm import ScriptedLLM, text, tool_results, tools
from tests.support.tool_e2e import smart_message, smart_rig
from tests.support.tool_fields import unused
from tests.unit.services.drive_mirror_fakes import AREA, ScriptedDrive

MEDIA_BUCKET = "media"

# --- the scripted delegations (each is one `context`) --------------------------------------------------------------
PLAN_TEXT = "Garden shed: planning permission decision expected by 2026-10-30.\n"
SAVE_TEXT = {"text": PLAN_TEXT, "name": "shed-plan.md", "folder": "Projects/Garden"}
LEASE_TEXT = b"Lisbon flat: rent 950 EUR per month, deposit 1,900 EUR.\n"
SAVE_FILE = {"file_ref": "lease.txt", "name": "lisbon-lease.txt", "folder": "Contracts"}
CREATE_FOLDER = {"folder": "Contracts/2026"}
MOVE = {"file_ref": "drive:<id>", "folder": "Home/Warranties", "new_name": "boiler-warranty-WB-55-1093.md"}
APPEND = {"file_ref": "drive:<id>", "append_text": "Decision received 2026-10-28."}
REPLACE = {"file_ref": "drive:<id>", "source_ref": "lease.txt"}
DELETE = {"file_ref": "drive:<id>"}
REFUSED_SAVE = {"file_ref": "lease.txt", "text": PLAN_TEXT}
REFUSED_MOVE = {"file_ref": "drive:<id>"}


class TreeDrive(ScriptedDrive):
    """`ScriptedDrive` plus the two reads `UserDriveService` also makes (it leaves them unimplemented)."""

    async def list_children(self, user_id: str, folder_id: str) -> List[DriveItem]:
        self._call("list_children", item_id=folder_id)
        self._folder(folder_id)
        return [self._item(n) for n in self.children(folder_id)]

    async def download(self, user_id: str, item_id: str) -> bytes:
        self._call("download", item_id=item_id)
        node = self.nodes.get(item_id)
        if node is None or node.is_folder:
            raise DriveItemNotFoundError(item_id)
        return node.data


def delegate(intent: str, context: dict, query: str = "") -> Tuple[str, dict]:
    return ("delegate_to_specialist", {"intent": intent, "query": query or f"{intent} for the owner", "context": context})


class World:
    def __init__(self) -> None:
        self.drive = TreeDrive()
        self.gcs = FakeGcs(buckets={MEDIA_BUCKET: "plain"})
        self.storage = GcsFileStorageAdapter(MEDIA_BUCKET)
        self.storage._client = self.gcs.client()
        self.channels = RecordingChannelFactory()
        state = FakeNotificationState(NOW)
        self.state = state
        self.notifier = UserNotificationService(state, self.channels, coordinator=None, notification_sla={})
        registry = AgentRegistry()
        registry.register(FILE_MANAGEMENT)
        self.coordinator = AgentCoordinator(registry=registry)
        self.agent = FileManagementAgent(
            config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                               capabilities={}, metadata={}),
            conversion_service=FileConversionService(storage=self.storage), storage=self.storage,
            notification=self.notifier, drive_service=UserDriveService(self.drive),
            localization=FileLocalizationAdapter(), language_service=FakeLanguageService(),
        )
        self.coordinator.register_agent(self.agent)

    async def start(self) -> "World":
        await self.state.save_primary("u1", "slack", "C1")
        await self.storage.upload(LEASE_TEXT, "lease.txt", "u1", "text/plain")
        return self

    def seed(self, path: str, data: bytes) -> str:
        """A file already on the drive; returns its `drive:` ref."""
        parent = AREA
        *folders, name = path.split("/")
        for folder in folders:
            found = self.drive.child(parent, folder)
            parent = found.item_id if found else self.drive.add_folder(parent, folder)
        return f"drive:{self.drive.add_file(parent, name, data)}"

    def content(self, path: str) -> Optional[bytes]:
        node = self.drive.find(path)
        return None if node is None else node.data

    def receipts(self) -> List[str]:
        return [t for _, _, t in self.channels.sent]

    async def run(self, llm: Any, prompt: str = "Keep this on my drive."):
        return await smart_rig(llm, coordinator=self.coordinator).execute(smart_message(prompt))


def with_ref(context: dict, ref: str, **over: Any) -> dict:
    return {**context, "file_ref": ref, **over}


def results_of(llm: Any) -> List[Tuple[str, Any]]:
    """The tool results the model was told, once each, in order. All recorded requests alias the engine's one growing
    message list, so card A's `tool_results` (the last message of each request) would repeat the last result: read the
    final request message by message with the same decoding."""
    final = llm.requests[-1]
    return tool_results(SimpleNamespace(requests=[SimpleNamespace(messages=[m]) for m in final.messages]))


def consumed(llm: Any) -> bool:
    return llm.unanswered == 0 and llm.pending() == 0


# --------------------------------------------------------------------------------------------------------- tests

async def test_save_text_with_name_and_folder():
    world = await World().start()
    llm = ScriptedLLM([tools(delegate(Intent.SAVE_FILE_TO_DRIVE, SAVE_TEXT)), text("Saved.")])

    response = await world.run(llm)

    assert response.status == AgentStatus.SUCCESS
    assert world.drive.files() == ["Projects/Garden/shed-plan.md"]
    assert world.content("Projects/Garden/shed-plan.md") == PLAN_TEXT.encode()
    (_, told), = results_of(llm)
    assert "Saved [Drive: Projects/Garden/shed-plan.md" in told
    assert consumed(llm)


async def test_save_a_chat_file_with_name_and_folder():
    world = await World().start()
    llm = ScriptedLLM([tools(delegate(Intent.SAVE_FILE_TO_DRIVE, SAVE_FILE)), text("Saved.")])

    await world.run(llm)

    assert world.drive.files() == ["Contracts/lisbon-lease.txt"]
    assert world.content("Contracts/lisbon-lease.txt") == LEASE_TEXT
    assert consumed(llm)


async def test_create_folder():
    world = await World().start()
    llm = ScriptedLLM([tools(delegate(Intent.CREATE_FOLDER_IN_DRIVE, CREATE_FOLDER)), text("Created.")])

    await world.run(llm)

    folders = sorted(world.drive.path_of(n.item_id) for n in world.drive.nodes.values() if n.is_folder and n.item_id != AREA)
    assert folders == ["Contracts", "Contracts/2026"]
    assert world.drive.files() == []
    assert consumed(llm)


async def test_move_with_folder_and_new_name():
    world = await World().start()
    ref = world.seed("Inbox/boiler-warranty.md", b"Boiler warranty WB-55-1093.\n")
    item_id = ref.split(":", 1)[1]
    llm = ScriptedLLM([tools(delegate(Intent.MOVE_FILE_IN_DRIVE, with_ref(MOVE, ref))), text("Moved.")])

    await world.run(llm)

    assert world.drive.path_of(item_id) == "Home/Warranties/boiler-warranty-WB-55-1093.md"    # same id, new place and name
    assert world.drive.files() == ["Home/Warranties/boiler-warranty-WB-55-1093.md"]
    assert world.content("Home/Warranties/boiler-warranty-WB-55-1093.md") == b"Boiler warranty WB-55-1093.\n"
    assert consumed(llm)


async def test_update_append_text_and_replace_with_source_ref():
    world = await World().start()
    shed = world.seed("Projects/Garden/shed-plan.md", PLAN_TEXT.encode())
    lease = world.seed("Contracts/lisbon-lease.txt", b"Old lease text.\n")
    llm = ScriptedLLM([
        tools(delegate(Intent.UPDATE_FILE_IN_DRIVE, with_ref(APPEND, shed))),
        tools(delegate(Intent.UPDATE_FILE_IN_DRIVE, with_ref(REPLACE, lease))),
        text("Updated both."),
    ])

    await world.run(llm)

    assert world.content("Projects/Garden/shed-plan.md") == PLAN_TEXT.encode() + b"Decision received 2026-10-28.\n"
    assert world.content("Contracts/lisbon-lease.txt") == LEASE_TEXT
    assert any(t.startswith("✏️ Replaced: Contracts/lisbon-lease.txt") for t in world.receipts())
    assert consumed(llm)


async def test_delete_from_drive():
    world = await World().start()
    ref = world.seed("Home/Warranties/boiler-warranty.md", b"Boiler warranty WB-55-1093.\n")
    llm = ScriptedLLM([tools(delegate(Intent.DELETE_FILE_FROM_DRIVE, with_ref(DELETE, ref))), text("Deleted.")])

    await world.run(llm)

    assert world.drive.files() == []
    assert "🗑️ Deleted from the drive: Home/Warranties/boiler-warranty.md" in world.receipts()
    assert consumed(llm)


async def test_a_save_with_both_a_file_and_text_is_refused_and_nothing_is_written():
    world = await World().start()
    llm = ScriptedLLM([tools(delegate(Intent.SAVE_FILE_TO_DRIVE, REFUSED_SAVE)), text("That did not work.")])

    await world.run(llm)

    (_, told), = results_of(llm)
    assert 'exactly one of "file_ref"' in told
    assert world.drive.files() == [] and world.drive.writes() == []
    assert consumed(llm)


async def test_a_move_with_neither_folder_nor_new_name_is_refused_and_nothing_moves():
    world = await World().start()
    ref = world.seed("Inbox/boiler-warranty.md", b"Boiler warranty WB-55-1093.\n")
    llm = ScriptedLLM([tools(delegate(Intent.MOVE_FILE_IN_DRIVE, with_ref(REFUSED_MOVE, ref))), text("Not moved.")])

    await world.run(llm)

    (_, told), = results_of(llm)
    assert "Give a destination folder, a new name, or both" in told
    assert world.drive.files() == ["Inbox/boiler-warranty.md"] and world.drive.writes() == []
    assert consumed(llm)


# ------------------------------------------------------------------------------------------------ declared fields

def test_the_scripted_calls_use_every_declared_field():
    assert unused("delegate", Intent.SAVE_FILE_TO_DRIVE, [SAVE_TEXT, SAVE_FILE]) == []
    assert unused("delegate", Intent.MOVE_FILE_IN_DRIVE, [MOVE]) == []
    assert unused("delegate", Intent.CREATE_FOLDER_IN_DRIVE, [CREATE_FOLDER]) == []
    assert unused("delegate", Intent.UPDATE_FILE_IN_DRIVE, [APPEND, REPLACE]) == []
    assert unused("delegate", Intent.DELETE_FILE_FROM_DRIVE, [DELETE]) == []
