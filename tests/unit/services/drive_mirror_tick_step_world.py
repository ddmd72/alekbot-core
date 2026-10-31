"""A tick harness with the transcript mirror's STEP wired (USER_DRIVE_INDEXING_RFC §27.9.5; plan Task 21C): the real
DriveIndexingService, DriveTranscriptStage and DriveTranscriptMirror on the in-memory repositories. The mirror writes to
the same fake drive the indexer reads (`EchoDrive`), so every folder and file it creates, moves or rewrites comes back
in the change feed, as Graph's own writes do. Synthetic items only."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_index import DriveChange, DriveChildrenPage
from src.domain.drive_index_params import TRANSCRIBE_USD_PER_MONTH
from src.domain.language import LanguageCode
from src.domain.user_drive import DriveItem, DriveItemNotFoundError, DriveNameConflictError, name_key
from src.ports.language_service_port import LanguageServicePort
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_transcript_mirror import DriveTranscriptMirror
from src.services.localization_service import LocalizationService

from .drive_index_fakes import ROOT
from .drive_mirror_tick_world import MirrorWorld
from .test_drive_indexing_transcripts import SPACE
from .drive_index_fakes import LegacyConnections

AREA_PATH = "Apps/Alek"
WRITES = ("create_folder", "upload", "replace_content", "move", "delete")


class FixedLanguage(LanguageServicePort):
    """The user's UI language, as `LanguagePreferenceService.resolve_ui_language` answers it. `fail`: it raises."""

    def __init__(self, lang: LanguageCode = LanguageCode.EN) -> None:
        self.lang = lang
        self.fail: Optional[Exception] = None
        self.calls = 0

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.lang

    async def get_preference(self, user_id: str) -> Tuple[Optional[LanguageCode], bool]:
        return self.lang, True


class EchoDrive:
    """The mirror's `UserDrivePort` side of the world's FakeContentDrive: Graph's write semantics as S1 measured them (a
    create fails on a taken name, case-insensitive after NFC; a move fails on a taken name; 404 for a missing item or
    parent), every write logged into the fake's change feed. `fail[method]`: exceptions raised by the next calls of
    that method (`when` filters by name). Every call is recorded in `calls`."""

    def __init__(self, drive: Any, clock: Any) -> None:
        self.drive = drive
        self.clock = clock
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.fail: Dict[str, List[Tuple[Exception, Any]]] = {}

    # -- helpers ---------------------------------------------------------------------------------------------------

    def _fault(self, method: str, name: str = "") -> None:
        queue = self.fail.get(method) or []
        for i, (exc, when) in enumerate(queue):
            if when is None or when(name):
                del queue[i]
                raise exc

    def _parent(self, parent_id: str) -> None:
        if parent_id != ROOT and (parent_id not in self.drive.items or not self.drive.items[parent_id].is_folder):
            raise DriveItemNotFoundError(parent_id)

    def _child(self, parent_id: str, name: str) -> Optional[DriveChange]:
        key = name_key(name)
        return next((c for c in self.drive.items.values() if c.parent_id == parent_id and name_key(c.name) == key),
                    None)

    def path_of(self, item_id: str) -> str:
        parts: List[str] = []
        while item_id in self.drive.items:
            ch = self.drive.items[item_id]
            parts.append(ch.name)
            item_id = ch.parent_id
        return "/".join(reversed(parts))

    def _item(self, item_id: str) -> DriveItem:
        ch = self.drive.items[item_id]
        return DriveItem(item_id, ch.name, self.path_of(item_id), ch.is_folder, ch.size_bytes,
                         identity=ch.identity, drive_identity=self.drive.drive_identity,
                         content_version=ch.content_version)

    def writes(self) -> List[Tuple[str, Dict[str, Any]]]:
        return [c for c in self.calls if c[0] in WRITES]

    def files(self) -> List[str]:
        """Every file's path, from the drive root's children down."""
        return sorted(self.path_of(i) for i, c in self.drive.items.items() if not c.is_folder)

    def find(self, path: str) -> Optional[DriveChange]:
        return next((c for i, c in self.drive.items.items() if self.path_of(i) == path), None)

    def content(self, path: str) -> bytes:
        found = next(i for i in self.drive.items if self.path_of(i) == path)
        return self.drive.content[found]

    # -- writes ----------------------------------------------------------------------------------------------------

    async def create_folder(self, handle: Any, parent_id: str, name: str) -> DriveItem:
        self.calls.append(("create_folder", {"parent_id": parent_id, "name": name}))
        self._fault("create_folder", name)
        self._parent(parent_id)
        if self._child(parent_id, name) is not None:
            raise DriveNameConflictError(name)
        item_id = self.drive.add(name, parent_id, folder=True, created_at=self.clock())
        return self._item(item_id)

    async def upload(self, handle: Any, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: str = "rename") -> DriveItem:
        self.calls.append(("upload", {"parent_id": parent_id, "name": filename, "conflict": conflict}))
        self._fault("upload", filename)
        self._parent(parent_id)
        existing = self._child(parent_id, filename)
        if existing is not None:
            if conflict == "fail":
                raise DriveNameConflictError(filename)
            raise AssertionError("the mirror creates with conflict=fail only")
        item_id = self.drive.add_file(filename, data, parent=parent_id, created_at=self.clock(),
                                      mime_type=content_type)
        return self._item(item_id)

    async def replace_content(self, handle: Any, item_id: str, data: bytes, content_type: str) -> DriveItem:
        self.calls.append(("replace_content", {"item_id": item_id}))
        self._fault("replace_content")
        if item_id not in self.drive.items or self.drive.items[item_id].is_folder:
            raise DriveItemNotFoundError(item_id)
        self.drive.edit(item_id, data)
        return self._item(item_id)

    async def move(self, handle: Any, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        self.calls.append(("move", {"item_id": item_id, "parent_id": new_parent_id, "name": new_name}))
        self._fault("move", new_name or "")
        if item_id not in self.drive.items:
            raise DriveItemNotFoundError(item_id)
        current = self.drive.items[item_id]
        parent = new_parent_id or current.parent_id
        self._parent(parent)
        name = new_name or current.name
        other = self._child(parent, name)
        if other is not None and other.item_id != item_id:
            raise DriveNameConflictError(name)
        self.drive.items[item_id] = replace(current, parent_id=parent, name=name)
        self.drive.log.append(self.drive.items[item_id])
        return self._item(item_id)

    async def delete(self, handle: Any, item_id: str) -> None:
        self.calls.append(("delete", {"item_id": item_id}))
        if item_id not in self.drive.items:
            raise DriveItemNotFoundError(item_id)
        self.drive.delete(item_id)

    # -- reads -----------------------------------------------------------------------------------------------------

    async def child_by_name(self, handle: Any, parent_id: str, name: str) -> Optional[DriveChange]:
        self.calls.append(("child_by_name", {"parent_id": parent_id, "name": name}))
        return self._child(parent_id, name)

    async def area_path(self, handle: Any) -> str:
        self.calls.append(("area_path", {}))
        return AREA_PATH

    async def get_change(self, handle: Any, item_id: str) -> DriveChange:
        self.calls.append(("get_change", {"item_id": item_id}))
        return await self.drive.get_change(handle, item_id)

    async def list_children_changes(self, handle: Any, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        self.calls.append(("list_children_changes", {"item_id": folder_id}))
        return await self.drive.list_children_changes(handle, folder_id, limit, cursor)

    async def get_root(self, handle: Any) -> DriveItem:
        self.calls.append(("get_root", {}))
        return DriveItem(ROOT, "", "", True, identity=f"{self.drive.drive_identity}:{ROOT}",
                         drive_identity=self.drive.drive_identity)

    async def capabilities(self, handle: Any) -> DriveCapabilities:
        """The connection's capabilities: the mirror reads its path limit (`max_path_chars`, MULTI_DRIVE_RFC §7.1)."""
        return DriveCapabilities(max_path_chars=400)


class StepWorld(MirrorWorld):
    """MirrorWorld whose service also carries the mirror STEP (`built`), as `indexer_main` wires it with the kill switch
    on: the step over the echoing drive, the localization service and a fixed UI language. `built=False`: the ledger
    only, as with the switch off. `runs` lists every `run()` call's host facts."""

    def __init__(self, tmp_path: Any, *, switch: bool = True, flag: bool = True, built: bool = True,
                 enrich: bool = False, lang: LanguageCode = LanguageCode.EN,
                 cap: float = TRANSCRIBE_USD_PER_MONTH) -> None:
        super().__init__(tmp_path, switch=switch, flag=flag, enrich=enrich, cap=cap)
        self.writes = EchoDrive(self.drive, self.clock)
        self.lang = FixedLanguage(lang)
        self.step = DriveTranscriptMirror(self.repo, self.mirror, self.writes) if built else None
        self.runs: List[Dict[str, Any]] = []
        self.outs: List[Dict[str, int]] = []
        self.budgets: List[Any] = []
        if self.step is not None:
            real_run = self.step.run

            async def run(handle: Any, host: Any) -> Dict[str, int]:
                self.runs.append({"drive_identity": host.drive_identity, "language": host.language, "tz": host.tz_name,
                                  "round_complete": host.round_complete, "epoch_idle": host.epoch_idle,
                                  "time_left": host.time_left(), "ops": host.ops_left(), "reads": host.reads_left(),
                                  "stamps": host.stamps_left(), "at": self.clock.monotonic()})
                out = await real_run(handle, host)
                self.outs.append(out)
                return out
            self.step.run = run   # type: ignore[method-assign]
        self.svc = DriveIndexingService(
            self.drive, self.repo, self.emb, self.queue, self.users, lambda lat, lon: "Rome (Roma), Italy", SPACE,
            extractors={"parser": self.parser, "reader": self.reader}, triage=self.triage,
            profiler=self.profiler if enrich else None, passages=self.passages if enrich else None,
            transcripts=self.stage, transcribe_enabled=switch, transcribe_cap_usd=cap, mirror_repo=self.mirror,
            mirror=self.step, localization=LocalizationService(FileLocalizationAdapter()) if built else None,
            language=self.lang if built else None, connections=LegacyConnections(self.users, self.drive), clock=self.clock, monotonic=self.clock.monotonic)
        real_work_off = self.svc._work_off

        async def work_off(handle, state, now, budget, summary, models):
            self.budgets.append(budget)
            return await real_work_off(handle, state, now, budget, summary, models)
        self.svc._work_off = work_off   # type: ignore[method-assign]

    def mirror_files(self) -> List[str]:
        return [p for p in self.writes.files() if p.startswith("_Transcripts/")]
