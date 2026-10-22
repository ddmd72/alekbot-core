"""STUB (part A) — FileManagementAgent's drive code, one instance per user (DRIVE_SPECIALIST_RFC §5.5).

Part A owns the real module (moved handlers, error mapping, per-user lock, shield, still-running, receipts,
late-failure notices). This stub keeps only the surface part B wires and tests against: the constructor, the public
coroutine methods (first argument the caller's AgentMessage, keyword arguments as below) and the two name sets. Each
stub method calls the service the real one calls, so a write that is not stubbed reaches the drive service.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, FrozenSet, Optional

from ..domain.agent import AgentMessage
from ..domain.drive_op import DriveOp, DriveOpOutcome, DriveOutcome
from ..domain.user_drive import parse_drive_ref

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.drive_search_service import DriveSearchService
    from ..services.file_conversion_service import FileConversionService
    from ..services.localization_service import LocalizationService
    from ..services.user_drive_service import UserDriveService
    from ..services.user_notification_service import UserNotificationService


class DriveOperations:
    """STUB (part A): the drive operations of one user."""

    # Every public coroutine method is in exactly one of the two sets (pinned by the harness-safety test).
    READ_METHODS: ClassVar[FrozenSet[str]] = frozenset({"search", "search_deleted", "list_folder", "open"})
    MUTATION_METHODS: ClassVar[FrozenSet[str]] = frozenset({
        "copy", "save_text", "create_folder", "move", "append", "replace", "delete"})
    OP_OF: ClassVar[dict] = {
        "search": DriveOp.SEARCH, "search_deleted": DriveOp.SEARCH, "list_folder": DriveOp.LIST, "open": DriveOp.OPEN,
        "copy": DriveOp.COPY, "save_text": DriveOp.SAVE_TEXT, "create_folder": DriveOp.CREATE_FOLDER,
        "move": DriveOp.MOVE, "append": DriveOp.APPEND, "replace": DriveOp.REPLACE, "delete": DriveOp.DELETE}

    def __init__(self, *, conversion_service: "FileConversionService", drive_service: Optional["UserDriveService"],
                 drive_search: Optional["DriveSearchService"] = None,
                 notification: Optional["UserNotificationService"] = None,
                 localization: Optional["LocalizationService"] = None,
                 language_service: Optional["LanguageServicePort"] = None) -> None:
        self.conversion_service = conversion_service
        self.drive_service = drive_service
        self.drive_search = drive_search
        self.notification = notification
        self.localization = localization
        self.language_service = language_service

    @property
    def configured(self) -> bool:
        return self.drive_service is not None

    def _uid(self, message: AgentMessage) -> str:
        return str(message.context.get("user_id", ""))

    async def search(self, message: AgentMessage, *, query: str = "", folder: str = "", kind: str = "",
                     date_from: str = "", date_to: str = "") -> DriveOpOutcome:
        return DriveOpOutcome(DriveOp.SEARCH, DriveOutcome.OK, f"search {query}")

    async def search_deleted(self, message: AgentMessage, *, query: str = "") -> DriveOpOutcome:
        return DriveOpOutcome(DriveOp.SEARCH, DriveOutcome.OK, f"deleted search {query}")

    async def list_folder(self, message: AgentMessage, *, folder: str = "") -> DriveOpOutcome:
        assert self.drive_service is not None
        out = await self.drive_service.list_folder(self._uid(message), folder or None)
        return DriveOpOutcome(DriveOp.LIST, DriveOutcome.OK, "listing", (out.folder, *out.items))

    async def open(self, message: AgentMessage, *, file_ref: str, original: bool = False) -> DriveOpOutcome:
        return DriveOpOutcome(DriveOp.OPEN, DriveOutcome.OK, f"opened {file_ref}")

    async def copy(self, message: AgentMessage, *, source_ref: str, folder: str = "", file_name: str = "",
                   wait_s: float = 100.0) -> DriveOpOutcome:
        assert self.drive_service is not None
        data = await self.conversion_service.resolve_bytes(source_ref, self._uid(message))
        out = await self.drive_service.save(self._uid(message), data, file_name or source_ref,
                                            "application/octet-stream", folder or None)
        return DriveOpOutcome(DriveOp.COPY, DriveOutcome.OK, "saved", (out.item,))

    async def save_text(self, message: AgentMessage, *, text: str, file_name: str, folder: str = "",
                        wait_s: float = 100.0) -> DriveOpOutcome:
        assert self.drive_service is not None
        out = await self.drive_service.save(self._uid(message), text.encode(), file_name, "text/markdown",
                                            folder or None)
        return DriveOpOutcome(DriveOp.SAVE_TEXT, DriveOutcome.OK, "saved", (out.item,))

    async def create_folder(self, message: AgentMessage, *, folder: str, wait_s: float = 100.0) -> DriveOpOutcome:
        assert self.drive_service is not None
        out = await self.drive_service.ensure_folder(self._uid(message), folder)
        return DriveOpOutcome(DriveOp.CREATE_FOLDER, DriveOutcome.OK, "created", (out.folder,))

    async def move(self, message: AgentMessage, *, file_ref: str, folder: str = "", new_name: str = "",
                   wait_s: float = 100.0) -> DriveOpOutcome:
        assert self.drive_service is not None
        out = await self.drive_service.move(self._uid(message), parse_drive_ref(file_ref) or "", folder or None,
                                            new_name or None)
        return DriveOpOutcome(DriveOp.MOVE, DriveOutcome.OK, "moved", (out.after,))

    async def append(self, message: AgentMessage, *, file_ref: str, text: str, wait_s: float = 100.0) -> DriveOpOutcome:
        assert self.drive_service is not None
        out = await self.drive_service.append_text(self._uid(message), parse_drive_ref(file_ref) or "", text)
        return DriveOpOutcome(DriveOp.APPEND, DriveOutcome.OK, "appended", (out.item,))

    async def replace(self, message: AgentMessage, *, target_ref: str, source_ref: str,
                      wait_s: float = 100.0) -> DriveOpOutcome:
        assert self.drive_service is not None
        data = await self.conversion_service.resolve_bytes(source_ref, self._uid(message))
        out = await self.drive_service.replace_with(self._uid(message), parse_drive_ref(target_ref) or "", data,
                                                    "application/octet-stream")
        return DriveOpOutcome(DriveOp.REPLACE, DriveOutcome.OK, "replaced", (out.item,))

    async def delete(self, message: AgentMessage, *, file_ref: str, wait_s: float = 100.0) -> DriveOpOutcome:
        assert self.drive_service is not None
        await self.drive_service.delete(self._uid(message), parse_drive_ref(file_ref) or "")
        return DriveOpOutcome(DriveOp.DELETE, DriveOutcome.OK, "deleted")

