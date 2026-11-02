"""The attached list of files the bot cannot open prints canonical paths (multi-drive 2a, design §1 S-13): each line
starts with the mount name of the connection whose drive the file is on, whatever the drive is called now."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import List

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, RecordState, Scope
from src.domain.drive_search import INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER
from src.domain.language import LanguageCode
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService
from tests.support.drive_connection_service_fake import single_connection_repository
from tests.unit.services.drive_index_fakes import T0, Clock, FakeQueue

MARKED = T0 + timedelta(minutes=1)


class Notifier:
    def __init__(self) -> None:
        self.files: List[dict] = []

    async def notify_file_bytes(self, user_id, account_id, file_bytes, filename, title, **kw) -> bool:
        self.files.append({"bytes": file_bytes, "filename": filename})
        return True

    async def notify_raw(self, user_id, account_id, text, **kw) -> bool:
        return True


class Localization:
    def get_ui_string(self, lang, message):
        return "{count} closed"


class Language:
    async def resolve_ui_language(self, user_id):
        return LanguageCode.UK


def closed(item_id, name, parent="root", folder=False):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=name, parent_id=parent,
                            is_folder=folder, kind="folder" if folder else "pdf", scope=Scope.NORMAL,
                            state=RecordState.METADATA_ONLY if folder else RecordState.PENDING, has_card=True,
                            inaccessible_since=MARKED, inaccessible_checked_at=MARKED)


def _env(mount_name: str):
    clock = Clock()
    clock.now = T0 + timedelta(hours=1)
    repo = InMemoryDriveIndexRepository()
    conns = single_connection_repository("u")
    stored = conns.connections[("u", "u")]
    conns.connections[("u", "u")] = replace(stored, mount_name=mount_name)
    notifier = Notifier()
    svc = DriveSyncSchedulerService(repo=repo, queue=FakeQueue(), connections=conns, notifier=notifier,
                                    alert_sink=None, clock=clock, localization=Localization(), language=Language())
    return SimpleNamespace(svc=svc, repo=repo, notifier=notifier, clock=clock)


async def _run(env, *recs):
    await env.repo.upsert_records(list(recs))
    env.repo.states["u"] = DriveSyncState(
        user_id="u", connection_id="u", root_item_id="root", observed_as_of=MARKED + timedelta(minutes=10),
        created_at=T0, last_success_at=env.clock.now,
        counters={INACCESSIBLE_COUNTER: len(recs), INACCESSIBLE_UNNOTIFIED_COUNTER: len(recs)})
    await env.svc.fan_out()
    return env.notifier.files[0]["bytes"].decode("utf-8").splitlines()


@pytest.mark.parametrize("mount", ["Drive", "Acme"])
async def test_every_listed_path_starts_with_the_mount_name(mount):
    env = _env(mount)
    folder = closed("d1", "R&D", folder=True)
    lines = await _run(env, folder, closed("f1", "Tom's plan.docx", parent="d1"), closed("f2", "menu.pdf"))
    assert lines == [f"{mount}/menu.pdf", f"{mount}/R&D/Tom's plan.docx"]
