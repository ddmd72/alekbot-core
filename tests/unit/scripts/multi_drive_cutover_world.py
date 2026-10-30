"""The world the cut-over script tests run on (MULTI_DRIVE_RFC §12, T-10, T-14): the live shapes of slice 1a in an
evaluating Firestore fake (`EvalDb`, with the index refusal of config/firestore.indexes.json switched on), a fake for
the queue, the Scheduler jobs and the index states, and the in-memory connection repository.

One user `usr-1` with one OneDrive credential `cred-1` on drive `drv-1`, an indexed drive under the old per-user keys,
two stored transcripts (one by hash, one by the item fallback, the first in two parts), a `_Transcripts` mirror with a
file, a folder and three archive folders, a note, a usage month and an open initial build. Technical ids only.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.drive_connection import DriveConnection
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.drive_mirror import archive_folder_key
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "infrastructure" / "multi_drive_cutover.py"
INDEXES = ROOT / "config" / "firestore.indexes.json"

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
USER, CRED, DRIVE, ACCOUNT = "usr-1", "cred-1", "drv-1", "acc-1"
REALM, SUBJECT = "microsoft", "sub-1"
ONEDRIVE = "microsoft_onedrive"
HASH_KEY = "QXH1aGFzaA==:100"                     # an old content key: <quickXorHash>:<size>
ITEM_KEY = "item:item-2:c:{V2},1"                 # an old fallback key: item:{itemId}:{cTag}
ARCHIVED_KEY = "item:item-9:c:{V9},1"
DAY = "2026-10-01"


def load_script() -> Any:
    """The script as a module (registered, so its dataclasses resolve their annotations), loaded once."""
    if "multi_drive_cutover" in sys.modules:
        return sys.modules["multi_drive_cutover"]
    spec = importlib.util.spec_from_file_location("multi_drive_cutover", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["multi_drive_cutover"] = module
    spec.loader.exec_module(module)
    return module


def names(script: Any) -> Any:
    p = "development_"
    return script.Names(credentials=f"{p}oauth_credentials", users=f"{p}users", connections=f"{p}drive_connections",
                        settings=f"{p}drive_user_settings", records=f"{p}drive_index_records",
                        chunks=f"{p}drive_index_chunks", hints=f"{p}drive_index_hints",
                        jobs=f"{p}drive_transcript_jobs", windows=f"{p}drive_transcript_windows",
                        batches=f"{p}drive_transcript_batches", sync_state=f"{p}drive_sync_state",
                        transcripts=f"{p}drive_transcripts", parts=f"{p}drive_transcript_parts",
                        roots=f"{p}drive_mirror_roots", ledger=f"{p}drive_transcript_mirror",
                        notes=f"{p}drive_notes", usage=f"{p}drive_model_usage")


@dataclass
class FakeInfra:
    paused: bool = True
    tasks: int = 0
    jobs: Dict[str, bool] = field(default_factory=lambda: {"start-drive-sync": True, "reconnect-notice-sweep": True})
    missing: List[str] = field(default_factory=list)
    asked: List[Sequence[Mapping[str, Any]]] = field(default_factory=list)

    async def queue(self) -> Tuple[bool, int]:
        return self.paused, self.tasks

    async def jobs_paused(self) -> Dict[str, bool]:
        return dict(self.jobs)

    async def missing_indexes(self, required: Sequence[Mapping[str, Any]]) -> List[str]:
        self.asked.append(required)
        return list(self.missing)


class FakeConnectionStore:
    """The in-memory repository plus the two deletes --purge-new makes, applied to its stored dicts."""

    def __init__(self) -> None:
        self.repo = FakeDriveConnectionRepository()

    async def list_for_user(self, user_id: str) -> List[DriveConnection]:
        return await self.repo.list_for_user(user_id)

    async def create(self, connection: DriveConnection) -> bool:
        return await self.repo.create(connection)

    async def get_settings(self, user_id: str) -> Optional[DriveUserSettings]:
        return await self.repo.get_settings(user_id)

    async def save_settings(self, settings: DriveUserSettings) -> None:
        await self.repo.save_settings(settings)

    async def remove(self, connection: DriveConnection) -> None:
        self.repo.connections.pop((connection.user_id, connection.connection_id), None)

    async def remove_settings(self, user_id: str) -> None:
        self.repo.settings.pop(user_id, None)


@dataclass
class World:
    script: Any
    db: EvalDb
    names: Any
    infra: FakeInfra
    store: FakeConnectionStore
    ids: List[str] = field(default_factory=lambda: ["conn1a2b3c4d", "conn9e8f7a6b"])
    tags: List[str] = field(default_factory=lambda: ["a1", "b2"])

    def run(self, now: datetime = NOW, owner: str = "cutover-test") -> Any:
        ids, tags = list(self.ids), list(self.tags)
        return self.script.Cutover(db=self.db, names=self.names, connections=self.store, infra=self.infra,
                                   required_indexes=[], now=now, new_id=lambda: ids.pop(0),
                                   new_tag=lambda: tags.pop(0), owner=owner)

    async def go(self, mode: str, *, write: bool = True, now: datetime = NOW,
                 owner: str = "cutover-test") -> Tuple[int, List[str]]:
        """One run of the script; each process has its own lease owner, so a second run passes another `owner`."""
        return await self.script.run_cutover(self.run(now, owner), mode, write=write)

    def col(self, name: str) -> Dict[str, Dict[str, Any]]:
        return self.db.collection(name).docs

    def doc(self, name: str, doc_id: str) -> Optional[Dict[str, Any]]:
        return self.db.collection(name).docs.get(doc_id)

    def sub(self, name: str, doc_id: str, sub: str) -> Dict[str, Dict[str, Any]]:
        return self.db.collection(f"{name}/{doc_id}/{sub}").docs

    def snapshot(self) -> Dict[str, Any]:
        stored = {name: copy.deepcopy(col.docs) for name, col in self.db.cols.items() if col.docs}
        return {"db": stored, "connections": dict(self.store.repo.connections),
                "settings": dict(self.store.repo.settings)}


def archive_key(drive: str, day: str, path: str) -> str:
    return archive_folder_key(USER, drive, day, path)


def seed(db: EvalDb, n: Any) -> None:
    """The live shapes before the cut-over (slice 1a)."""
    def put(name: str, doc_id: str, data: Dict[str, Any]) -> None:
        db.collection(name).docs[doc_id] = copy.deepcopy(data)

    put(n.credentials, f"{USER}:{CRED}", {"user_id": USER, "credential_id": CRED, "kind": ONEDRIVE,
                                          "integration": "drive", "account_id": ACCOUNT,
                                          "account": {"realm": REALM, "subject": SUBJECT}, "drive_id": DRIVE,
                                          "secret": "sealed-cred-1"})
    put(n.credentials, f"{USER}:cred-9", {"user_id": USER, "credential_id": "cred-9", "kind": "gmail",
                                          "account": {"realm": "google", "subject": "sub-9"}, "secret": "sealed"})
    put(n.users, USER, {"user_id": USER, "config": {"drive_indexing_enabled": True,
                                                    "drive_transcribe_enabled": True, "tier": "balanced"}})
    put(n.sync_state, USER, {"user_id": USER, "drive_id": DRIVE, "epoch": 3, "lease_owner": None,
                             "lease_until": None, "purging": False, "purge_keep_transcripts": False})
    for item in ("item-1", "item-2"):
        put(n.records, f"{USER}:{item}", {"user_id": USER, "item_id": item, "transcript_state": "current",
                                          "transcript_key": HASH_KEY if item == "item-1" else ITEM_KEY})
        put(n.chunks, f"{USER}:{item}:1:0", {"user_id": USER, "item_id": item, "generation": 1, "index": 0})
        put(n.hints, f"{USER}:{item}:1:0", {"user_id": USER, "item_id": item, "generation": 1, "index": 0})
    put(n.jobs, f"{USER}:item-2", {"user_id": USER, "item_id": "item-2", "content_key": ITEM_KEY,
                                   "reserved_usd": 0.0})
    put(f"{n.jobs}/{USER}:item-2/{n.windows}", "0", {"user_id": USER, "item_id": "item-2", "index": 0})
    put(n.batches, f"{USER}:batches%2Fb1", {"user_id": USER, "name": "batches/b1", "state": "done",
                                            "reserved_usd": 0.0})
    put(n.transcripts, f"{USER}:{HASH_KEY}", {"user_id": USER, "content_key": HASH_KEY, "parts": 2,
                                              "summary": "a call", "has_transcript": True, "last_used_at": EARLIER})
    for i in range(2):
        put(f"{n.transcripts}/{USER}:{HASH_KEY}/{n.parts}", f"t1-{i}",
            {"user_id": USER, "index": i, "token": "t1", "segments": [{"start_s": i, "text": f"part {i}"}]})
    put(n.transcripts, f"{USER}:{ITEM_KEY}", {"user_id": USER, "content_key": ITEM_KEY, "parts": 0,
                                              "segments": [], "last_used_at": EARLIER})
    put(n.transcripts, f"{USER}:{ARCHIVED_KEY}", {"user_id": USER, "content_key": ARCHIVED_KEY, "parts": 0,
                                                  "last_used_at": EARLIER})
    put(n.roots, f"{USER}:{DRIVE}", {"user_id": USER, "drive_id": DRIVE, "root_item_id": "root-1",
                                     "root_name": "_Transcripts"})
    base = {"user_id": USER, "drive_id": DRIVE, "state": "active"}
    put(n.ledger, f"{USER}:folder:fold-1", {**base, "kind": "folder", "source_item_id": "fold-1",
                                            "mirror_item_id": "mf-1", "mirror_parent_id": "root-1"})
    put(n.ledger, f"{USER}:file:item-1", {**base, "kind": "file", "source_item_id": "item-1",
                                          "content_key": HASH_KEY, "mirror_item_id": "m-1",
                                          "mirror_parent_id": "mf-1"})
    for name, key, item, parent in (("Archive", archive_key(DRIVE, "", "Archive"), "a-0", "root-1"),
                                    (DAY, archive_key(DRIVE, DAY, ""), "a-1", "a-0"),
                                    ("Calls", archive_key(DRIVE, DAY, "Calls"), "a-2", "a-1")):
        put(n.ledger, key, {**base, "kind": "archive", "source_item_id": key.rsplit(":", 1)[1],
                            "source_name": name, "mirror_item_id": item, "mirror_parent_id": parent})
    put(n.ledger, f"{USER}:file:item-9", {**base, "kind": "file", "source_item_id": "item-9", "state": "archived",
                                          "content_key": ARCHIVED_KEY, "mirror_item_id": "m-9",
                                          "mirror_parent_id": "a-2"})
    put(n.notes, f"{USER}:n1", {"user_id": USER, "note_id": "n1", "state": "active",
                                "match_keys": [f"i:{ONEDRIVE}:{DRIVE}:item-1", "r:item-1"]})
    put(n.usage, f"{USER}:initial", {"user_id": USER, "allowance_usd": 40.0, "spent_usd": 12.5,
                                     "opened_at": EARLIER, "closed_at": None})
    put(n.usage, f"{USER}:2026-10", {"user_id": USER, "transcribe_usd": 3.0, "transcribe_reserved_usd": 0.0})


def world() -> World:
    script = load_script()
    db = EvalDb(indexes=INDEXES)
    n = names(script)
    seed(db, n)
    return World(script=script, db=db, names=n, infra=FakeInfra(), store=FakeConnectionStore())


def later(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)
