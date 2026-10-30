"""P14 card for the drive specialist: every declared field of its writing tools and of the commission, end to end
(TEST_COVERAGE_SYSTEMIC_RFC §5 P14; DRIVE_SPECIALIST_RFC §4.2, §5.5, §8.1).

Smart's scripted `delegate_to_specialist(work_with_drive_files, query, {file_contents})` goes through the real
`DelegationEngine` and a real `AgentCoordinator` (registry with `DRIVE_SPECIALIST`) into the real `DriveAgent`, whose own
scripted model calls its local tools: the real `DriveToolRun` handlers, the user's real `DriveOperations` and the real
`UserDriveService`, over a drive fake with Graph's write semantics (`ScriptedDrive`, given the two read methods
`UserDriveService` also uses) and the chat-file store on the GCS fake. Receipts go through the real
`UserNotificationService` to recording channels. Assertions are on the drive's tree and bytes, on the receipts (sent
literally), on what the specialist's model was told and on the answer Smart's model was given.

Every declared field of the `drive` surface's writing tools and of `delegate:work_with_drive_files` is sent by a
scripted call (module constants; the last test checks them against the declarations). This card replaces
`test_file_management_drive_tool_fields_e2e.py` (the five mutating Smart-era drive intents, removed by RFC §4.1): each
stored effect and refusal that file proved is proved here through the specialist's tool that now carries it.
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.gcs_file_storage_adapter import GcsFileStorageAdapter
from src.agents.drive_agent import DriveAgent
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentStatus
from src.domain.drive_handle import DriveHandle
from src.domain.user_drive import MOVE_NEEDS_TARGET, DriveItem, DriveItemNotFoundError
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import DRIVE_SPECIALIST, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from src.services.user_notification_service import UserNotificationService
from tests.support.drive_service_connections import seeded_connections
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import FakeLanguageService, FakeNotificationState, RecordingChannelFactory
from tests.support.gcs_fake import FakeGcs
from tests.support.scripted_llm import ScriptedLLM, execution_context, text, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder, smart_message, smart_rig
from tests.support.tool_fields import unused
from tests.unit.services.drive_mirror_fakes import AREA, ScriptedDrive

MEDIA_BUCKET = "media"
USER = "u1"

# --- the commission's one field (`file_contents`, the delegate surface) --------------------------------------------
PLAN_TEXT = "Garden shed: planning permission decision expected by 2026-10-30.\n"
DECISION = "Decision received 2026-10-28."
NEW_FILE = {"name": "shed-plan-draft.md", "text": PLAN_TEXT}
APPEND_ITEM = {"target": "drive:<id>", "text": DECISION}      # the target ref exists only after seeding: see appended()
LEASE_TEXT = b"Lisbon flat: rent 950 EUR per month, deposit 1,900 EUR.\n"

# --- the specialist's scripted tool calls (the drive surface) -------------------------------------------------------
SAVE_TEXT = {"text_item": "t1", "file_name": "shed-plan.md", "folder": "Projects/Garden"}
SAVE_TEXT_ITEM_NAME = {"text_item": "t1"}                    # file_name empty: the item's own name, default Inbox
COPY = {"source_ref": "lease.txt", "file_name": "lisbon-lease.txt", "folder": "Contracts"}
CREATE_FOLDER = {"folder": "Contracts/2026"}
MOVE = {"file_ref": "drive:<id>", "folder": "Home/Warranties", "new_name": "boiler-warranty-WB-55-1093.md"}
APPEND = {"text_item": "t1", "file_ref": "drive:<id>"}
APPEND_TO_ITEM_TARGET = {"text_item": "t1"}                  # file_ref empty: the item's target
REPLACE = {"target_ref": "drive:<id>", "new_content_ref": "lease.txt"}
DELETE = {"file_ref": "drive:<id>"}
REPORT = {"note": "Did what was asked."}
REPORT_QUESTION = {"note": "Found two warranties.", "question": "Which warranty: Inbox/boiler-warranty.md or "
                                                                "Home/boiler-warranty.md?"}
REPORT_NOT_DONE = {"note": "Nothing changed.", "not_done": "restoring a deleted file"}
REFUSED_UNKNOWN_ITEM = {"text_item": "t7", "file_name": "shed-plan.md"}
REFUSED_MOVE = {"file_ref": "drive:<id>"}


class TreeDrive(ScriptedDrive):
    """`ScriptedDrive` plus the two reads `UserDriveService` also makes (it leaves them unimplemented)."""

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        self._call("list_children", item_id=folder_id)
        self._folder(folder_id)
        return [self._item(n) for n in self.children(folder_id)]

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        self._call("download", item_id=item_id)
        node = self.nodes.get(item_id)
        if node is None or node.is_folder:
            raise DriveItemNotFoundError(item_id)
        return node.data


def delegate(context: Optional[dict] = None, query: str = "Keep this on the owner's drive.") -> Tuple[str, dict]:
    """Smart's one drive commission."""
    args: Dict[str, Any] = {"intent": Intent.WORK_WITH_DRIVE_FILES, "query": query}
    if context is not None:
        args["context"] = context
    return ("delegate_to_specialist", args)


def with_ref(call: dict, **refs: str) -> dict:
    return {**call, **refs}


def appended(ref: str) -> dict:
    """The append item of `file_contents`, aimed at a seeded file."""
    return {**APPEND_ITEM, "target": ref}


class World:
    def __init__(self) -> None:
        self.drive = TreeDrive()
        connections = seeded_connections(USER)   # the user's one primary connection, tag a1 (MULTI_DRIVE_RFC §4.4)
        self.gcs = FakeGcs(buckets={MEDIA_BUCKET: "plain"})
        self.storage = GcsFileStorageAdapter(MEDIA_BUCKET)
        self.storage._client = self.gcs.client()
        self.channels = RecordingChannelFactory()
        self.state = FakeNotificationState(NOW)
        notifier = UserNotificationService(self.state, self.channels, coordinator=None, notification_sla={})
        self.operations = DriveOperations(
            conversion_service=FileConversionService(storage=self.storage, drive=self.drive, connections=connections),
            notification=notifier, drive_service=UserDriveService(self.drive, connections),
            localization=FileLocalizationAdapter(),
            language_service=FakeLanguageService(),
        )
        registry = AgentRegistry()
        registry.register(DRIVE_SPECIALIST)
        self.coordinator = AgentCoordinator(registry=registry)

    async def start(self) -> "World":
        await self.state.save_primary(USER, "slack", "C1")
        await self.storage.upload(LEASE_TEXT, "lease.txt", USER, "text/plain")
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

    def folders(self) -> List[str]:
        return sorted(self.drive.path_of(n.item_id) for n in self.drive.nodes.values()
                      if n.is_folder and n.item_id != AREA)

    def receipts(self) -> List[str]:
        return [t for _, _, t in self.channels.sent]

    def literal_receipts(self) -> List[str]:
        return [t for _, _, t in self.channels.sent_literal]

    async def run(self, smart: ScriptedLLM, specialist: ScriptedLLM, prompt: str = "Keep this on my drive."):
        agent = DriveAgent(
            AgentConfig(agent_id=f"drive_agent_{USER}", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
            execution_context=execution_context(specialist, "drive"), operations=self.operations,
            prompt_builder=StubPromptBuilder("DriveAgent prompt"),
        )
        self.coordinator.register_agent(agent)
        return await smart_rig(smart, coordinator=self.coordinator).execute(smart_message(prompt))


def smart_was_told(smart: ScriptedLLM) -> str:
    """The specialist's answer, as Smart's model received it (its one tool result)."""
    (_, told), = tool_results(smart)
    return told


def specialist_was_told(specialist: ScriptedLLM) -> List[str]:
    """What the specialist's model read back from its tools, in order."""
    return [str(r) for _, r in tool_results(specialist)]


def consumed(*llms: ScriptedLLM) -> bool:
    return all(llm.unanswered == 0 and llm.pending() == 0 for llm in llms)


def specialist(*turns: Sequence[Tuple[str, dict]], report: dict = REPORT) -> ScriptedLLM:
    """The specialist's script: each turn's tool calls, then its report."""
    return ScriptedLLM([*(tools(*turn) for turn in turns), tools((tt.REPORT_TOOL, report))])


# --------------------------------------------------------------------------------------------------------- tests

async def test_save_text_writes_the_commissions_item_with_file_name_and_folder():
    world = await World().start()
    smart = ScriptedLLM([tools(delegate({"file_contents": [NEW_FILE]})), text("Saved.")])
    drive = specialist([(tt.SAVE_TEXT_FILE, SAVE_TEXT)])

    response = await world.run(smart, drive)

    assert response.status == AgentStatus.SUCCESS
    assert world.drive.files() == ["Projects/Garden/shed-plan.md"]
    assert world.content("Projects/Garden/shed-plan.md") == PLAN_TEXT.encode()     # the item's text, not retyped
    assert 't1 name "shed-plan-draft.md"' in drive.requests[0].system_instruction        # the state block lists the item
    (seen,) = specialist_was_told(drive)
    assert "Saved [Drive: Projects/Garden/shed-plan.md" in seen
    told = smart_was_told(smart)
    assert told.startswith("Drive result: ok\n")
    assert "1. done: Saved [Drive: Projects/Garden/shed-plan.md" in told
    assert consumed(smart, drive)


async def test_save_text_without_file_name_uses_the_items_name_and_inbox():
    world = await World().start()
    smart = ScriptedLLM([tools(delegate({"file_contents": [NEW_FILE]})), text("Saved.")])
    drive = specialist([(tt.SAVE_TEXT_FILE, SAVE_TEXT_ITEM_NAME)])

    await world.run(smart, drive)

    assert world.drive.files() == ["Inbox/shed-plan-draft.md"]
    assert world.content("Inbox/shed-plan-draft.md") == PLAN_TEXT.encode()
    assert consumed(smart, drive)


async def test_copy_a_chat_file_with_file_name_and_folder():
    world = await World().start()
    smart = ScriptedLLM([tools(delegate()), text("Saved.")])
    drive = specialist([(tt.COPY_FILE_TO_DRIVE, COPY)])

    await world.run(smart, drive)

    assert world.drive.files() == ["Contracts/lisbon-lease.txt"]
    assert world.content("Contracts/lisbon-lease.txt") == LEASE_TEXT
    assert "1. done: Saved [Drive: Contracts/lisbon-lease.txt" in smart_was_told(smart)
    assert consumed(smart, drive)


async def test_create_folder():
    world = await World().start()
    smart = ScriptedLLM([tools(delegate()), text("Created.")])
    drive = specialist([(tt.CREATE_FOLDER, CREATE_FOLDER)])

    await world.run(smart, drive)

    assert world.folders() == ["Contracts", "Contracts/2026"]
    assert world.drive.files() == []
    assert "Created: Contracts, Contracts/2026." in smart_was_told(smart)
    assert consumed(smart, drive)


async def test_move_with_folder_and_new_name():
    world = await World().start()
    ref = world.seed("Inbox/boiler-warranty.md", b"Boiler warranty WB-55-1093.\n")
    item_id = ref.split(":", 1)[1]
    smart = ScriptedLLM([tools(delegate()), text("Moved.")])
    drive = specialist([(tt.MOVE_FILE, with_ref(MOVE, file_ref=ref))])

    await world.run(smart, drive)

    assert world.drive.path_of(item_id) == "Home/Warranties/boiler-warranty-WB-55-1093.md"    # same id, new place and name
    assert world.drive.files() == ["Home/Warranties/boiler-warranty-WB-55-1093.md"]
    assert world.content("Home/Warranties/boiler-warranty-WB-55-1093.md") == b"Boiler warranty WB-55-1093.\n"
    assert [m for m, _ in world.drive.writes()].count("move") == 1                              # one provider call
    assert consumed(smart, drive)


async def test_append_the_commissions_item_by_file_ref_and_by_the_items_target():
    world = await World().start()
    shed = world.seed("Projects/Garden/shed-plan.md", PLAN_TEXT.encode())
    notes = world.seed("Projects/Garden/notes.md", b"Shed notes.\n")
    smart = ScriptedLLM([
        tools(delegate({"file_contents": [appended(notes)]})),
        tools(delegate({"file_contents": [appended(shed)]})),
        text("Updated both."),
    ])
    drive = ScriptedLLM([
        tools((tt.APPEND_TO_FILE, with_ref(APPEND, file_ref=shed))),          # commission 1: file_ref wins over the target
        tools((tt.REPORT_TOOL, REPORT)),
        tools((tt.APPEND_TO_FILE, APPEND_TO_ITEM_TARGET)),                    # commission 2: the item's own target
        tools((tt.REPORT_TOOL, REPORT)),
    ])

    await world.run(smart, drive)

    assert world.content("Projects/Garden/shed-plan.md") == (PLAN_TEXT + DECISION + "\n" + DECISION + "\n").encode()
    assert world.content("Projects/Garden/notes.md") == b"Shed notes.\n"
    assert [m for m, _ in world.drive.writes()] == ["replace_content", "replace_content"]
    assert consumed(smart, drive)


async def test_replace_with_a_chat_file_posts_a_literal_receipt():
    world = await World().start()
    lease = world.seed("Contracts/lisbon-lease.txt", b"Old lease text.\n")
    smart = ScriptedLLM([tools(delegate()), text("Replaced.")])
    drive = specialist([(tt.REPLACE_FILE_CONTENT, with_ref(REPLACE, target_ref=lease))])

    await world.run(smart, drive)

    assert world.content("Contracts/lisbon-lease.txt") == LEASE_TEXT
    (receipt,) = world.literal_receipts()
    assert receipt.startswith("✏️ Replaced: Contracts/lisbon-lease.txt")
    assert world.receipts() == [receipt]
    assert consumed(smart, drive)


async def test_replace_with_another_drive_file():
    world = await World().start()
    old = world.seed("Contracts/lisbon-lease.txt", b"Old lease text.\n")
    new = world.seed("Inbox/lisbon-lease-2026.txt", LEASE_TEXT)
    smart = ScriptedLLM([tools(delegate()), text("Replaced.")])
    drive = specialist([(tt.REPLACE_FILE_CONTENT, {"target_ref": old, "new_content_ref": new})])

    await world.run(smart, drive)

    assert world.content("Contracts/lisbon-lease.txt") == LEASE_TEXT
    assert world.content("Inbox/lisbon-lease-2026.txt") == LEASE_TEXT                 # the source is left as it was
    assert consumed(smart, drive)


async def test_delete_from_drive_posts_a_literal_receipt():
    world = await World().start()
    ref = world.seed("Home/Warranties/boiler-warranty.md", b"Boiler warranty WB-55-1093.\n")
    smart = ScriptedLLM([tools(delegate()), text("Deleted.")])
    drive = specialist([(tt.DELETE_DRIVE_ITEM, with_ref(DELETE, file_ref=ref))])

    await world.run(smart, drive)

    assert world.drive.files() == []
    assert world.literal_receipts() == ["🗑️ Deleted from the drive: Home/Warranties/boiler-warranty.md"]
    assert world.receipts() == world.literal_receipts()
    assert "1. done: Deleted Home/Warranties/boiler-warranty.md." in smart_was_told(smart)
    assert consumed(smart, drive)


async def test_the_report_note_reaches_smart():
    world = await World().start()
    smart = ScriptedLLM([tools(delegate()), text("Created.")])
    drive = specialist([(tt.CREATE_FOLDER, CREATE_FOLDER)], report=REPORT)

    await world.run(smart, drive)

    assert "Specialist note: Did what was asked." in smart_was_told(smart)
    assert consumed(smart, drive)


async def test_the_report_question_reaches_smart_as_the_status_and_what_to_do():
    world = await World().start()
    smart = ScriptedLLM([tools(delegate()), text("Which one?")])
    drive = ScriptedLLM([tools((tt.REPORT_TOOL, REPORT_QUESTION))])

    await world.run(smart, drive)

    told = smart_was_told(smart)
    assert told.startswith("Drive result: ambiguous\nWhat to do: Which warranty: Inbox/boiler-warranty.md or "
                           "Home/boiler-warranty.md?")
    assert "Specialist note: Found two warranties." in told
    assert world.drive.writes() == []
    assert consumed(smart, drive)


async def test_the_report_not_done_reaches_smart_as_not_possible():
    world = await World().start()
    smart = ScriptedLLM([tools(delegate()), text("That cannot be done.")])
    drive = ScriptedLLM([tools((tt.REPORT_TOOL, REPORT_NOT_DONE))])

    await world.run(smart, drive)

    told = smart_was_told(smart)
    assert told.startswith("Drive result: not_possible\n")
    assert "restoring a deleted file" in told.splitlines()[1]
    assert world.drive.writes() == []
    assert consumed(smart, drive)


# ------------------------------------------------------------------------------------------------------- refusals

async def test_a_save_naming_no_item_is_refused_and_nothing_is_written():
    # Today's twin of the old "both a chat file and text" refusal: the specialist cannot write content that the
    # commission did not carry. A file and text together are now two tools (copy_file_to_drive, save_text_file).
    world = await World().start()
    smart = ScriptedLLM([tools(delegate({"file_contents": [NEW_FILE]})), text("That did not work.")])
    drive = specialist([(tt.SAVE_TEXT_FILE, REFUSED_UNKNOWN_ITEM)])

    await world.run(smart, drive)

    (seen,) = specialist_was_told(drive)
    assert "t7" in seen and "t1" in seen                        # the unknown value and the items that exist
    assert world.drive.files() == [] and world.drive.writes() == []
    assert smart_was_told(smart).startswith("Drive result: not_possible\n")
    assert consumed(smart, drive)


async def test_a_copy_of_a_drive_file_is_refused_and_nothing_is_written():
    # The old card's "a save with both a file and text" refused a mixed save shape; the specialist's copy refuses the
    # one source a copy must not take (a file already on the drive is moved, not copied).
    world = await World().start()
    ref = world.seed("Inbox/boiler-warranty.md", b"Boiler warranty WB-55-1093.\n")
    smart = ScriptedLLM([tools(delegate()), text("That did not work.")])
    drive = specialist([(tt.COPY_FILE_TO_DRIVE, {"source_ref": ref})])

    await world.run(smart, drive)

    (seen,) = specialist_was_told(drive)
    assert f"'{ref}' is already on the drive; use move_file." in seen
    assert world.drive.files() == ["Inbox/boiler-warranty.md"] and world.drive.writes() == []
    assert consumed(smart, drive)


async def test_a_move_with_neither_folder_nor_new_name_is_refused_and_nothing_moves():
    world = await World().start()
    ref = world.seed("Inbox/boiler-warranty.md", b"Boiler warranty WB-55-1093.\n")
    smart = ScriptedLLM([tools(delegate()), text("Not moved.")])
    drive = specialist([(tt.MOVE_FILE, with_ref(REFUSED_MOVE, file_ref=ref))])

    await world.run(smart, drive)

    (seen,) = specialist_was_told(drive)
    assert MOVE_NEEDS_TARGET in seen
    assert "Give a destination folder, a new name, or both" in seen
    assert world.drive.files() == ["Inbox/boiler-warranty.md"] and world.drive.writes() == []
    assert consumed(smart, drive)


# ------------------------------------------------------------------------------------------------ declared fields

def test_the_scripted_calls_use_every_declared_field():
    assert unused("delegate", Intent.WORK_WITH_DRIVE_FILES,
                  [{"file_contents": [NEW_FILE]}, {"file_contents": [APPEND_ITEM]}]) == []
    assert unused("drive", tt.SAVE_TEXT_FILE, [SAVE_TEXT, SAVE_TEXT_ITEM_NAME]) == []
    assert unused("drive", tt.COPY_FILE_TO_DRIVE, [COPY]) == []
    assert unused("drive", tt.CREATE_FOLDER, [CREATE_FOLDER]) == []
    assert unused("drive", tt.MOVE_FILE, [MOVE]) == []
    assert unused("drive", tt.APPEND_TO_FILE, [APPEND, APPEND_TO_ITEM_TARGET]) == []
    assert unused("drive", tt.REPLACE_FILE_CONTENT, [REPLACE]) == []
    assert unused("drive", tt.DELETE_DRIVE_ITEM, [DELETE]) == []
    assert unused("drive", tt.REPORT_TOOL, [REPORT, REPORT_QUESTION, REPORT_NOT_DONE]) == []
