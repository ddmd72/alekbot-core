"""
The slice-1b cut-over on live data (docs/10_rfcs/MULTI_DRIVE_RFC.md §12, §10, I5-I8, E14, T-10, T-14): from one drive per
user to drive connections. One-off; deleted after acceptance.

Modes (exactly one; a dry run when none is given):
  (none) / --dry-run   the preflight and, per user, what --apply would do: counts per collection and the computed
                       initial-build key with that record's spent and closed state. Writes nothing.
  --apply              steps a-d of RFC §12 per user with a OneDrive credential, after a preflight that refuses unless
                       every check passes:
                       a. the DriveConnection (new id and tag, name `Drive`, the credential, the drive identity, both
                          flags copied from UserBotConfig) and the primary pointer;
                       b. purge with keep of the old per-user index: `purging` + `purge_keep_transcripts` written on the
                          old sync state under its lease FIRST (an old revision rolled back to mid-step finishes the
                          purge itself), then the transcript jobs (with their window results), batches, hints, chunks
                          and records whose ids carry the old `{user_id}:` prefix (never a connection-scoped id), then
                          the sync state `{user_id}`. The transcript store and its pins, notes, mirror roots and ledger,
                          and usage are not touched;
                       c. the copies of RFC §10: transcript heads with every `parts` document and `last_used_at` = now,
                          the mirror ledger (content keys and pins in the new form), the mirror roots, the initial-build
                          record; notes gain `r:{tag}:{item}`. Old documents are kept (rollback);
                       d. counts per collection.
                       Idempotent: a re-run writes nothing new (a copy whose target exists is skipped).
  --purge-new          rollback before transcription is back on: deletes only the connection-scoped documents of the
                       index collections, the connections' sync states, the connection documents and the user's drive
                       settings. Never the transcript store, ledger, mirror roots, notes or usage. Refused while
                       transcription work is in flight (the same drain rule as --apply).
  --copy-back          rollback after transcription is back on (run BEFORE --purge-new): transcript heads with their
                       parts and ledger documents under the new keys that the old keys lack, or hold differently, are
                       copied to the old keys (the old revision's own key shapes), so the old revision neither pays for
                       them again nor meets unknown files in `_Transcripts`; a note written since the cut-over gets
                       back its old `r:{item}` key.
                       Both rollback modes stamp `last_used_at` = now on every old-key transcript head (E22 on the
                       old keys: the old revision's orphan sweep cannot delete a paid transcript before its rebuilt
                       records carry the key again).
  --cleanup            after acceptance: deletes the old-key heads with their parts, the old ledger, root and
                       initial-build documents, the `r:{item}` note keys, the credentials' `drive_id` and the
                       UserBotConfig drive flags. An old document is deleted only when its new-key twin exists.
                       The `user_id` index forms are the owner's (gcloud), not this script's.
  --purge-new, --copy-back and --cleanup take --dry-run to count only.

Preflight (--apply; RFC §12 step 3): the drive queue is paused and empty, the Scheduler jobs `start_drive_sync` and
`reconnect_notice_sweep` are paused, no transcript job is in flight (no record `working` / `waiting`, no open batch)
and no reservation is open, every old sync state's lease is free, no connection has a sync state yet (so a re-run
after the resume can never purge the rebuilt index), the connection_id / drive_identity indexes of
config/firestore.indexes.json are READY, one OneDrive credential per user with its `drive_id`, and the I6 equality
checks: every mirror root's and ledger document's `drive_id`, and the drive part of every note's `i:` key, equal the
credential's `drive_id`; every archive folder document that has a folder on the drive can be re-keyed.
--copy-back and --cleanup check the queue and the two jobs only; --purge-new also the drain rule.
A run that crashed mid-purge holds the old lease for 30 minutes (the owner is per process): a re-run before then is
refused as "lease is held"; wait the 30 minutes, then re-run.

Keys (I6, T-14): every key this script writes is computed by the product's functions (the domain key functions and
the OneDrive adapter's identity and content-identity functions) from the old documents; the old key shapes are known
only to this script (E14), in the `legacy_*` functions below, used to read and, for --copy-back, to write the old keys.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/multi_drive_cutover.py [--user <user_id> ...] [--dry-run]
    python scripts/infrastructure/multi_drive_cutover.py --apply
    python scripts/infrastructure/multi_drive_cutover.py --purge-new | --copy-back | --cleanup [--dry-run]
    queue and jobs: --project (default $GOOGLE_CLOUD_PROJECT), --location us-central1, --queue drive-sync-dev,
    --job alek-bot-dev-start-drive-sync --job alek-bot-dev-reconnect-notice-sweep
Exit code: 0 when done (or a clean dry run), 1 when the preflight refused (or a dry run found a refusal reason).
Output: counts and the initial-build line only (ids are cut to 8 characters); nothing personal.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import firestore  # noqa: E402
from google.cloud.firestore_v1.transforms import DELETE_FIELD, ArrayRemove, ArrayUnion  # noqa: E402

from src.adapters.microsoft.onedrive_adapter import (  # noqa: E402
    content_identity_from_graph, onedrive_drive_identity, onedrive_item_identity,
)
from src.composition.connector_registry import MICROSOFT_ONEDRIVE, ConnectorRegistry  # noqa: E402
from src.domain.drive_connection import DEFAULT_MOUNT_NAME, DriveConnection, connection_doc_id  # noqa: E402
from src.domain.drive_connection_state import ConnectionState  # noqa: E402
from src.domain.drive_index import sync_state_doc_id  # noqa: E402
from src.domain.drive_mirror import (  # noqa: E402
    ARCHIVE_NAME, KIND_ARCHIVE, archive_folder_key, ledger_key, mirror_root_doc_id,
)
from src.domain.drive_note import BINDING_KEY_PREFIX, REF_KEY_PREFIX, item_binding_key, tagged_ref_key  # noqa: E402
from src.domain.drive_ref import TAG_ALPHABET, TAG_FIRST_ALPHABET  # noqa: E402
from src.domain.drive_transcript import BATCH_CLOSED_STATES, safe_doc_id, transcript_doc_id  # noqa: E402
from src.domain.drive_transcript_plan import WAITING, WORKING  # noqa: E402
from src.domain.drive_transcript_windows import transcript_content_key  # noqa: E402
from src.domain.drive_usage import initial_build_doc_id  # noqa: E402
from src.domain.drive_user_settings import DriveUserSettings  # noqa: E402

_HOLD = timedelta(minutes=30)       # the old lease, held across one user's purge
_WRITE_BATCH = 400                  # writes per batch commit (Firestore takes 500)
_LEASE_OWNER_PREFIX = "multi-drive-cutover-"
_IN_FLIGHT_STATES = (WORKING, WAITING)
_TOKEN_FIELDS = ("connection_id",)  # the projection of an id scan: small documents, never vectors or text

MODES = ("dry-run", "apply", "purge-new", "copy-back", "cleanup")


# -- collections -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Names:
    """Collection names (env-prefixed in production, `EnvironmentConfig`)."""
    credentials: str
    users: str
    connections: str
    settings: str
    records: str
    chunks: str
    hints: str
    jobs: str
    windows: str          # the jobs' subcollection
    batches: str
    sync_state: str
    transcripts: str
    parts: str            # the heads' subcollection
    roots: str
    ledger: str
    notes: str
    usage: str

    @classmethod
    def of(cls, env: Any) -> "Names":
        return cls(credentials=env.credentials_collection, users=env.user_collection_name,
                   connections=env.drive_connections_collection, settings=env.drive_user_settings_collection,
                   records=env.drive_index_records_collection, chunks=env.drive_index_chunks_collection,
                   hints=env.drive_index_hints_collection, jobs=env.drive_transcript_jobs_collection,
                   windows=env.drive_transcript_windows_subcollection,
                   batches=env.drive_transcript_batches_collection, sync_state=env.drive_sync_state_collection,
                   transcripts=env.drive_transcripts_collection, parts=env.drive_transcript_parts_subcollection,
                   roots=env.drive_mirror_roots_collection, ledger=env.drive_transcript_mirror_collection,
                   notes=env.drive_notes_collection, usage=env.drive_model_usage_collection)

    def index_collections(self) -> Tuple[str, ...]:
        """The connection-scoped index collections, in purge order (jobs first: their windows go with them)."""
        return (self.jobs, self.batches, self.hints, self.chunks, self.records)


# -- the old key shapes (E14: known only to this script) -----------------------------------------------------------

def legacy_prefix(user_id: str) -> str:
    """Every old per-user index document id starts with this (`{user_id}:`, escaped as the old ids were)."""
    return safe_doc_id(f"{user_id}:")


def legacy_initial_doc_id(user_id: str) -> str:
    return f"{user_id}:initial"


def legacy_ledger_key(user_id: str, kind: str, source_item_id: str) -> str:
    return f"{user_id}:{kind}:{source_item_id}"


def legacy_root_doc_id(user_id: str, drive_id: str) -> str:
    return f"{user_id}:{drive_id}"


def legacy_ref_key(item_id: str) -> str:
    return f"{REF_KEY_PREFIX}{item_id}"


@dataclass(frozen=True)
class LegacyHash:
    """An old content key `<quickXorHash>:<size>` (the unprefixed hash the new code never writes, D12)."""
    digest: str
    size: int


@dataclass(frozen=True)
class LegacyItem:
    """An old fallback content key `item:{itemId}:{cTag}`."""
    item_id: str
    version: str


def parse_legacy_content_key(key: str, id_space: str) -> Optional[Any]:
    """The old content key's parts, or None for a new-form key (or one of neither form)."""
    if key.startswith("item:"):
        rest = key[len("item:"):]
        if rest.startswith(f"{id_space}:"):
            return None
        item_id, sep, version = rest.partition(":")
        return LegacyItem(item_id, version) if sep and item_id else None
    parts = key.split(":")
    if len(parts) == 2 and parts[0] and parts[1].isdigit():
        return LegacyHash(parts[0], int(parts[1]))
    return None


def legacy_content_key(new_key: str, id_space: str, family: str) -> Optional[str]:
    """--copy-back: the old content key of a new-form one; None for a key of neither new form."""
    item_prefix = f"item:{id_space}:"
    if new_key.startswith(item_prefix):
        _drive, sep, tail = new_key[len(item_prefix):].partition(":")
        item_id, sep2, version = tail.partition(":")
        return f"item:{item_id}:{version}" if sep and sep2 and item_id else None
    if new_key.startswith(f"{family}:"):
        rest = new_key[len(family) + 1:]
        return rest if parse_legacy_content_key(rest, id_space) is not None else None
    return None


# -- the new keys (I6: the product's functions only) --------------------------------------------------------------

def content_family() -> str:
    """The content-identity family the OneDrive adapter gives a file with a hash (read from the adapter, not typed)."""
    probe = content_identity_from_graph({"file": {"hashes": {"quickXorHash": "probe"}}, "size": 0})
    if probe is None:
        raise RuntimeError("the OneDrive adapter built no content identity for a hashed file")
    return probe.family


def new_content_key(old_key: str, drive_id: str, id_space: str) -> Optional[str]:
    """The stored transcript's content key the runtime computes for the item the old key was made for (RFC §10):
    the adapter's content identity of `<hash>:<size>`, or the fallback by the item identity. None when `old_key` is not
    an old-form key."""
    parsed = parse_legacy_content_key(old_key, id_space)
    if isinstance(parsed, LegacyHash):
        identity = content_identity_from_graph({"file": {"hashes": {"quickXorHash": parsed.digest}},
                                                "size": parsed.size})
        return transcript_content_key(identity, "", "")[0]
    if isinstance(parsed, LegacyItem):
        return transcript_content_key(None, onedrive_item_identity(drive_id, parsed.item_id, id_space),
                                      parsed.version)[0]
    return None


def identity_item_id(identity: str, drive_id: str, id_space: str) -> str:
    """The provider item id of an adapter-built item identity on `drive_id`; "" when the adapter's own function does
    not give back exactly that identity for it (so a changed identity shape answers "", never a wrong id)."""
    item_id = identity.rsplit(":", 1)[-1]
    return item_id if item_id and onedrive_item_identity(drive_id, item_id, id_space) == identity else ""


# -- archive folder documents: their key hashes (drive, day, path), which the document does not store --------------

def archive_chains(docs: Mapping[str, Mapping[str, Any]], root_item_id: str) -> Dict[str, Tuple[str, str]]:
    """(day, path) of every archive folder document whose parent chain reaches the mirror root, from the names its
    folders were created under (`source_name`), as the mirror built the key: [`Archive`] or [`Long paths`] at the root
    -> ("", name); [`Archive`, day, *dirs] -> (day, "/".join(dirs)). A document is placed by its `mirror_parent_id`, or,
    for a create intent with no folder yet, its `pending_parent_id`. The caller verifies each pair against a key."""
    by_item = {d.get("mirror_item_id"): doc_id for doc_id, d in docs.items()
               if d.get("kind") == KIND_ARCHIVE and d.get("mirror_item_id")}
    out: Dict[str, Tuple[str, str]] = {}
    for doc_id, doc in docs.items():
        if doc.get("kind") != KIND_ARCHIVE:
            continue
        names = [str(doc.get("source_name") or "")]
        parent = doc.get("mirror_parent_id") or doc.get("pending_parent_id") or ""
        seen: Set[str] = set()
        while parent and parent != root_item_id and parent not in seen and parent in by_item:
            seen.add(parent)
            up = docs[by_item[parent]]
            names.append(str(up.get("source_name") or ""))
            parent = up.get("mirror_parent_id") or ""
        if parent != root_item_id:
            continue
        chain = list(reversed(names))
        if len(chain) == 1:
            out[doc_id] = ("", chain[0])
        elif chain[0] == ARCHIVE_NAME:
            out[doc_id] = (chain[1], "/".join(chain[2:]))
    return out


def rekey_archives(user_id: str, docs: Mapping[str, Mapping[str, Any]], root_item_id: str, from_drive: str,
                   to_drive: str) -> Tuple[Dict[str, str], List[str]]:
    """{current id: target id} of the archive folder documents among `docs` whose id is
    `archive_folder_key(user, from_drive, day, path)` for the (day, path) their chain gives, the target being the same
    with `to_drive`; and the ids that could not be verified."""
    chains = archive_chains(docs, root_item_id)
    mapping: Dict[str, str] = {}
    unresolved: List[str] = []
    for doc_id, doc in docs.items():
        if doc.get("kind") != KIND_ARCHIVE:
            continue
        pair = chains.get(doc_id)
        if pair is not None and archive_folder_key(user_id, from_drive, *pair) == doc_id:
            mapping[doc_id] = archive_folder_key(user_id, to_drive, *pair)
        else:
            unresolved.append(doc_id)
    return mapping, unresolved


# -- what the script needs outside Firestore ---------------------------------------------------------------------

class Infra(Protocol):
    """The queue, the Scheduler jobs and the index states (production: gcloud; tests: a fake)."""

    async def queue(self) -> Tuple[bool, int]:
        """(paused, tasks waiting)."""

    async def jobs_paused(self) -> Dict[str, bool]:
        """Scheduler job name -> paused."""

    async def missing_indexes(self, required: Sequence[Mapping[str, Any]]) -> List[str]:
        """A line per required index that is not READY."""


class ConnectionStore(Protocol):
    """The connections and the user's drive settings: the repository port for what the runtime writes (its codec),
    plus the two deletes only --purge-new makes (the port never deletes a connection, RFC §4.4)."""

    async def list_for_user(self, user_id: str) -> List[DriveConnection]: ...

    async def create(self, connection: DriveConnection) -> bool: ...

    async def get_settings(self, user_id: str) -> Optional[DriveUserSettings]: ...

    async def save_settings(self, settings: DriveUserSettings) -> None: ...

    async def remove(self, connection: DriveConnection) -> None: ...

    async def remove_settings(self, user_id: str) -> None: ...


class RepositoryConnectionStore:
    """`DriveConnectionRepository` for reads and writes, raw deletes by the product's document ids for --purge-new."""

    def __init__(self, repo: Any, db: Any, names: Names) -> None:
        self._repo, self._db, self._names = repo, db, names

    async def list_for_user(self, user_id: str) -> List[DriveConnection]:
        return list(await self._repo.list_for_user(user_id))

    async def create(self, connection: DriveConnection) -> bool:
        return bool(await self._repo.create(connection))

    async def get_settings(self, user_id: str) -> Optional[DriveUserSettings]:
        settings: Optional[DriveUserSettings] = await self._repo.get_settings(user_id)
        return settings

    async def save_settings(self, settings: DriveUserSettings) -> None:
        await self._repo.save_settings(settings)

    async def remove(self, connection: DriveConnection) -> None:
        await self._db.collection(self._names.connections).document(connection_doc_id(connection.scope)).delete()

    async def remove_settings(self, user_id: str) -> None:
        await self._db.collection(self._names.settings).document(user_id).delete()


# -- the run -----------------------------------------------------------------------------------------------------

@dataclass
class Counts:
    """Per collection: documents written / deleted / skipped, by step."""
    items: Dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        if n:
            self.items[key] = self.items.get(key, 0) + n

    def merge(self, other: "Counts") -> None:
        for key, n in other.items.items():
            self.add(key, n)

    def line(self) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(self.items.items())) or "nothing"


@dataclass
class Credential:
    """The plaintext fields of a OneDrive credential document (never a decrypt)."""
    doc_id: str
    user_id: str
    credential_id: str
    account_id: str
    realm: str
    subject: str
    drive_id: str


@dataclass
class Cutover:
    db: Any
    names: Names
    connections: ConnectionStore
    infra: Infra
    required_indexes: Sequence[Mapping[str, Any]]
    now: datetime
    kind: str = MICROSOFT_ONEDRIVE
    id_space: str = ""
    new_id: Callable[[], str] = field(default=lambda: secrets.token_hex(8))
    new_tag: Callable[[], str] = field(default=lambda: secrets.choice(TAG_FIRST_ALPHABET) + secrets.choice(TAG_ALPHABET))
    owner: str = field(default_factory=lambda: f"{_LEASE_OWNER_PREFIX}{secrets.token_hex(6)}")
    family: str = ""      # the adapter's content-identity family, read at construction

    def __post_init__(self) -> None:
        spec = ConnectorRegistry().get(self.kind)
        self.id_space = self.id_space or spec.id_space
        self.family = content_family()

    # -- reads ---------------------------------------------------------------------------------------------------

    def col(self, name: str) -> Any:
        return self.db.collection(name)

    async def docs_of(self, name: str, user_id: str, fields: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
        query = self.col(name).where(filter=firestore.FieldFilter("user_id", "==", user_id))
        if fields is not None:
            query = query.select(list(fields))
        return {snap.id: (snap.to_dict() or {}) async for snap in query.stream()}

    async def get(self, name: str, doc_id: str) -> Optional[Dict[str, Any]]:
        snap = await self.col(name).document(doc_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    async def subdocs(self, parent: str, doc_id: str, sub: str) -> Dict[str, Dict[str, Any]]:
        return {snap.id: (snap.to_dict() or {}) async for snap in self.col(f"{parent}/{doc_id}/{sub}").stream()}

    async def credentials(self) -> Dict[str, List[Credential]]:
        """user id -> the user's OneDrive credentials (plaintext fields)."""
        query = self.col(self.names.credentials).where(filter=firestore.FieldFilter("kind", "==", self.kind))
        out: Dict[str, List[Credential]] = {}
        async for snap in query.stream():
            data = snap.to_dict() or {}
            account = data.get("account") or {}
            cred = Credential(doc_id=snap.id, user_id=str(data.get("user_id") or ""),
                              credential_id=str(data.get("credential_id") or ""),
                              account_id=str(data.get("account_id") or ""), realm=str(account.get("realm") or ""),
                              subject=str(account.get("subject") or ""), drive_id=str(data.get("drive_id") or ""))
            out.setdefault(cred.user_id, []).append(cred)
        return out

    async def old_state_users(self) -> List[str]:
        """Users with an old sync state (doc id = the user id, no connection)."""
        out = []
        async for snap in self.col(self.names.sync_state).stream():
            data = snap.to_dict() or {}
            if not data.get("connection_id") and data.get("user_id") == snap.id:
                out.append(snap.id)
        return out

    async def old_index_ids(self, name: str, user_id: str) -> List[str]:
        prefix = legacy_prefix(user_id)
        docs = await self.docs_of(name, user_id, _TOKEN_FIELDS)
        return sorted(i for i, d in docs.items() if i.startswith(prefix) and not d.get("connection_id"))

    async def connection_index_ids(self, name: str, connection_id: str) -> List[str]:
        query = (self.col(name).where(filter=firestore.FieldFilter("connection_id", "==", connection_id))
                 .select(list(_TOKEN_FIELDS)))
        return sorted([snap.id async for snap in query.stream() if snap.id.startswith(f"{connection_id}:")])

    async def flags(self, user_id: str) -> Tuple[bool, bool]:
        user = await self.get(self.names.users, user_id) or {}
        config = user.get("config") or {}
        return bool(config.get("drive_indexing_enabled")), bool(config.get("drive_transcribe_enabled"))

    # -- writes --------------------------------------------------------------------------------------------------

    async def delete_ids(self, name: str, ids: Iterable[str], sub: Optional[str] = None) -> int:
        """Deletes the documents (each one's `sub` subcollection first) in batches; the count of parents deleted."""
        refs: List[Any] = []
        count = 0
        for doc_id in ids:
            if sub is not None:
                refs += [self.col(f"{name}/{doc_id}/{sub}").document(s)
                         for s in await self.subdocs(name, doc_id, sub)]
            refs.append(self.col(name).document(doc_id))
            count += 1
        await self._commit([("delete", ref, None) for ref in refs])
        return count

    async def _commit(self, writes: Sequence[Tuple[str, Any, Optional[Dict[str, Any]]]]) -> None:
        for start in range(0, len(writes), _WRITE_BATCH):
            batch = self.db.batch()
            for op, ref, data in writes[start:start + _WRITE_BATCH]:
                if op == "delete":
                    batch.delete(ref)
                elif op == "update":
                    batch.update(ref, data)
                else:
                    batch.set(ref, data)
            await batch.commit()


# -- preflight ---------------------------------------------------------------------------------------------------

async def infra_problems(run: Cutover, *, indexes: bool) -> List[str]:
    problems: List[str] = []
    paused, tasks = await run.infra.queue()
    if not paused:
        problems.append("the drive queue is not paused")
    if tasks:
        problems.append(f"the drive queue holds {tasks} tasks: purge it (old payloads carry no connection_id)")
    for job, is_paused in sorted((await run.infra.jobs_paused()).items()):
        if not is_paused:
            problems.append(f"the Scheduler job {job} is not paused")
    if indexes:
        problems += [f"index not READY: {line}" for line in await run.infra.missing_indexes(run.required_indexes)]
    return problems


def _busy(state: Mapping[str, Any], now: datetime, owner: Optional[str] = None) -> bool:
    until = state.get("lease_until")
    return bool(state.get("lease_owner") and state.get("lease_owner") != owner and until is not None and until > now)


async def user_problems(run: Cutover, user_id: str, creds: Sequence[Credential]) -> List[str]:
    """The per-user preflight (RFC §12 step 3), I6 included. Empty when the user may be cut over."""
    n = run.names
    short = user_id[:8]
    problems: List[str] = []
    if len(creds) > 1:
        problems.append(f"{short}: {len(creds)} OneDrive credentials (one expected before the cut-over)")
    cred = creds[0] if creds else None
    if cred is not None and not cred.drive_id:
        problems.append(f"{short}: the OneDrive credential has no drive_id")
    if cred is not None and not (cred.realm and cred.subject and cred.account_id and cred.credential_id):
        problems.append(f"{short}: the OneDrive credential lacks its account, account_id or credential_id")
    state = await run.get(n.sync_state, user_id)
    if state is not None and _busy(state, run.now):
        problems.append(f"{short}: the old sync state's lease is held (a tick runs, or a crashed run's lease has "
                        f"not run out: wait {int(_HOLD.total_seconds() // 60)} minutes)")
    problems += await in_flight_problems(run, user_id)
    for conn in await run.connections.list_for_user(user_id):
        if await run.get(n.sync_state, sync_state_doc_id(conn.scope)) is not None:
            problems.append(f"{short}: connection {conn.connection_id[:8]} already has a sync state "
                            f"(the index was rebuilt: never re-run --apply after the resume)")
    if cred is None or not cred.drive_id:
        return problems
    problems += await i6_problems(run, user_id, cred)
    return problems


async def in_flight_problems(run: Cutover, user_id: str) -> List[str]:
    """The drain rule (RFC §12): no transcript job working or waiting, no open batch, no open reservation, under any
    key of the user (old or connection-scoped). --apply and --purge-new both refuse otherwise: a purge would book a
    submitted batch as spend and drop it."""
    n, short = run.names, user_id[:8]
    problems: List[str] = []
    records = await run.docs_of(n.records, user_id, ("transcript_state", "connection_id"))
    live = sum(1 for d in records.values() if d.get("transcript_state") in _IN_FLIGHT_STATES)
    if live:
        problems.append(f"{short}: {live} recordings in transcript state working/waiting (a job is in flight)")
    batches = await run.docs_of(n.batches, user_id, ("state", "reserved_usd"))
    open_batches = sum(1 for d in batches.values() if d.get("state") not in BATCH_CLOSED_STATES)
    if open_batches:
        problems.append(f"{short}: {open_batches} open transcript batches")
    jobs = await run.docs_of(n.jobs, user_id, ("reserved_usd",))
    reserved = sum(1 for d in list(jobs.values()) + list(batches.values()) if float(d.get("reserved_usd") or 0) > 0)
    usage = await run.docs_of(n.usage, user_id)
    reserved += sum(1 for d in usage.values() for k, v in d.items()
                    if k.endswith("_reserved_usd") and float(v or 0) > 0)
    if reserved:
        problems.append(f"{short}: {reserved} open reservations")
    return problems


async def i6_problems(run: Cutover, user_id: str, cred: Credential) -> List[str]:
    """RFC §10 I6: the drive the old keys were made for is the credential's, so the new keys equal the runtime's."""
    n, short, drive = run.names, user_id[:8], cred.drive_id
    problems: List[str] = []
    roots = await run.docs_of(n.roots, user_id)
    old_roots = {i: d for i, d in roots.items() if not d.get("drive_identity")}
    if any(d.get("drive_id") != drive for d in old_roots.values()):
        problems.append(f"{short}: a mirror root's drive_id differs from the credential's")
    ledger = await run.docs_of(n.ledger, user_id)
    old_ledger = {i: d for i, d in ledger.items() if not d.get("drive_identity")}
    if any(d.get("drive_id") != drive for d in old_ledger.values()):
        problems.append(f"{short}: a ledger document's drive_id differs from the credential's")
    root = old_roots.get(legacy_root_doc_id(user_id, drive))
    if root is not None:
        _, unresolved = rekey_archives(user_id, old_ledger, str(root.get("root_item_id") or ""), drive,
                                       onedrive_drive_identity(drive, run.id_space))
        placed = [i for i in unresolved if old_ledger[i].get("mirror_item_id")]
        if placed:
            problems.append(f"{short}: {len(placed)} archive folder documents cannot be re-keyed (chain broken)")
    elif any(d.get("kind") == KIND_ARCHIVE for d in old_ledger.values()):
        problems.append(f"{short}: archive folder documents but no mirror root of the credential's drive")
    notes = await run.docs_of(n.notes, user_id, ("match_keys",))
    mismatched = sum(1 for d in notes.values() if note_item_id(d.get("match_keys") or (), drive, run.id_space) is False)
    if mismatched:
        problems.append(f"{short}: {mismatched} notes bound to another drive (their i: key's drive part differs)")
    return problems


def note_item_id(match_keys: Sequence[str], drive_id: str, id_space: str) -> Any:
    """The item id of a note bound on `drive_id`: the old `r:{item}` key whose item gives exactly the note's `i:` key.
    None for a note with no `i:` key (nothing to check, nothing to add); False when an `i:` key matches no `r:` key on
    that drive (I6 refuses)."""
    binding = [k for k in match_keys if k.startswith(BINDING_KEY_PREFIX)]
    if not binding:
        return None
    for key in match_keys:
        if key.startswith(REF_KEY_PREFIX):
            item_id = key[len(REF_KEY_PREFIX):]
            identity = onedrive_item_identity(drive_id, item_id, id_space)
            if identity and item_binding_key(identity) in binding:
                return item_id
    return False


# -- the steps of --apply -----------------------------------------------------------------------------------------

async def ensure_connection(run: Cutover, cred: Credential, counts: Counts, *, write: bool) -> Optional[DriveConnection]:
    """Step a: the user's connection of this credential (reused on a re-run) and the primary pointer."""
    existing = [c for c in await run.connections.list_for_user(cred.user_id)
                if c.kind == run.kind and c.credential_id == cred.credential_id]
    conn = existing[0] if existing else None
    if conn is None:
        indexing, transcribe = await run.flags(cred.user_id)
        conn = DriveConnection(connection_id=run.new_id(), user_id=cred.user_id, account_id=cred.account_id,
                               credential_id=cred.credential_id, kind=run.kind,
                               drive_identity=onedrive_drive_identity(cred.drive_id, run.id_space),
                               ref_tag=run.new_tag(), mount_name=DEFAULT_MOUNT_NAME, indexing_enabled=indexing,
                               transcribe_enabled=transcribe, state=ConnectionState.ACTIVE, created_at=run.now)
        counts.add("connections created")
        if write and not await run.connections.create(conn):
            raise RuntimeError(f"connection id {conn.connection_id[:8]} already taken")
    settings = await run.connections.get_settings(cred.user_id)
    if settings is None or not settings.primary_connection_id:
        counts.add("primary set")
        if write:
            await run.connections.save_settings(DriveUserSettings(user_id=cred.user_id,
                                                                  primary_connection_id=conn.connection_id))
    return conn


async def purge_old_index(run: Cutover, user_id: str, counts: Counts, *, write: bool) -> None:
    """Step b: purge with keep of the old per-user index, the purge flags first under the old lease."""
    n = run.names
    state_ref = run.col(n.sync_state).document(user_id)
    state = await run.get(n.sync_state, user_id)
    leased = False
    if state is not None and write:
        await _flag_purging(run, state_ref)
        leased = True
    for name in n.index_collections():
        ids = await run.old_index_ids(name, user_id)
        counts.add(f"deleted {name}", len(ids))
        if write and ids:
            await run.delete_ids(name, ids, n.windows if name == n.jobs else None)
    if state is not None:
        counts.add(f"deleted {n.sync_state}")
        if write and leased:
            await _delete_fenced(run, state_ref)


async def _flag_purging(run: Cutover, ref: Any) -> None:
    """`purging` + `purge_keep_transcripts` with the lease taken, in one transaction (the trigger today's rebuild
    writes): an old revision rolled back to now finishes the purge itself, keeping the store."""
    until = run.now + _HOLD

    @firestore.async_transactional
    async def _take(transaction: Any) -> None:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return
        if _busy(snap.to_dict() or {}, run.now, run.owner):
            raise RuntimeError("the old sync state's lease is held (a tick runs)")
        transaction.update(ref, {"lease_owner": run.owner, "lease_until": until, "purging": True,
                                 "purge_keep_transcripts": True})

    await _take(run.db.transaction())


async def _delete_fenced(run: Cutover, ref: Any) -> None:
    @firestore.async_transactional
    async def _delete(transaction: Any) -> None:
        snap = await ref.get(transaction=transaction)
        if snap.exists and (snap.to_dict() or {}).get("lease_owner") == run.owner:
            transaction.delete(ref)

    await _delete(run.db.transaction())


async def copy_transcripts(run: Cutover, cred: Credential, counts: Counts, *, write: bool) -> None:
    """Step c, the store: every old-key head with ALL its parts under the new key, `last_used_at` = now (E22)."""
    n = run.names
    for head_id, head in sorted((await run.docs_of(n.transcripts, cred.user_id)).items()):
        old_key = str(head.get("content_key") or "")
        new_key = new_content_key(old_key, cred.drive_id, run.id_space)
        if new_key is None:
            if parse_legacy_content_key(old_key, run.id_space) is None and not _is_new_key(run, old_key):
                counts.add(f"unrecognised {n.transcripts} keys")
            continue
        new_id = transcript_doc_id(cred.user_id, new_key)
        if await run.get(n.transcripts, new_id) is not None:
            counts.add(f"already copied {n.transcripts}")
            continue
        parts = await run.subdocs(n.transcripts, head_id, n.parts)
        counts.add(f"copied {n.transcripts}")
        counts.add(f"copied {n.parts}", len(parts))
        if not write:
            continue
        part_col = run.col(f"{n.transcripts}/{new_id}/{n.parts}")
        writes: List[Tuple[str, Any, Optional[Dict[str, Any]]]] = [
            ("set", part_col.document(pid), data) for pid, data in sorted(parts.items())]
        await run._commit(writes)   # the parts first: a head never names parts that are not there
        await run._commit([("set", run.col(n.transcripts).document(new_id),
                            {**head, "content_key": new_key, "last_used_at": run.now})])


def _is_new_key(run: Cutover, key: str) -> bool:
    return key.startswith(f"item:{run.id_space}:") or key.startswith(f"{run.family}:")


async def copy_mirror(run: Cutover, cred: Credential, counts: Counts, *, write: bool) -> None:
    """Step c, the mirror: roots by drive identity, ledger by (mirror drive identity, source identity) or the archive
    folder's hashed key with the drive identity; content keys (the pins) in the new form."""
    n, user_id, drive = run.names, cred.user_id, cred.drive_id
    identity = onedrive_drive_identity(drive, run.id_space)
    roots = {i: d for i, d in (await run.docs_of(n.roots, user_id)).items() if not d.get("drive_identity")}
    for root in roots.values():
        root_identity = onedrive_drive_identity(str(root.get("drive_id") or ""), run.id_space)
        await _copy_one(run, n.roots, mirror_root_doc_id(user_id, root_identity),
                        {**root, "drive_identity": root_identity}, counts, write=write)
    ledger = {i: d for i, d in (await run.docs_of(n.ledger, user_id)).items() if not d.get("drive_identity")}
    root = roots.get(legacy_root_doc_id(user_id, drive)) or {}
    archive_ids, unresolved = rekey_archives(user_id, ledger, str(root.get("root_item_id") or ""), drive, identity)
    counts.add(f"skipped {n.ledger} (archive intent without a folder, not re-keyable)", len(unresolved))
    for doc_id, doc in sorted(ledger.items()):
        data = {**doc, "drive_identity": identity}
        key = str(doc.get("content_key") or "")
        if key:
            data["content_key"] = new_content_key(key, drive, run.id_space) or key
        if doc.get("kind") == KIND_ARCHIVE:
            if doc_id not in archive_ids:
                continue
            target = archive_ids[doc_id]
            data["source_item_id"] = target.rsplit(":", 1)[1]
            data["source_identity"] = ""
        else:
            source = onedrive_item_identity(drive, str(doc.get("source_item_id") or ""), run.id_space)
            if not source:
                counts.add(f"skipped {n.ledger} (no source item)")
                continue
            data["source_identity"] = source
            target = ledger_key(user_id, str(doc.get("kind")), identity, source)
        await _copy_one(run, n.ledger, target, data, counts, write=write)


async def _copy_one(run: Cutover, name: str, target: str, data: Dict[str, Any], counts: Counts, *,
                    write: bool) -> None:
    if await run.get(name, target) is not None:
        counts.add(f"already copied {name}")
        return
    counts.add(f"copied {name}")
    if write:
        await run.col(name).document(target).set(data)


async def copy_initial_build(run: Cutover, cred: Credential, counts: Counts, *, write: bool) -> str:
    """Step c, the initial build: `{user}:initial` to the account's key with its spent and closed state (D8). Returns
    the dry-run line."""
    n = run.names
    target = initial_build_doc_id(cred.user_id, cred.realm, cred.subject)
    old = await run.get(n.usage, legacy_initial_doc_id(cred.user_id))
    line = f"initial build -> {target[:8]}…: "
    if old is None:
        return line + "no record (an unopened build applies)"
    state = "closed" if old.get("closed_at") else ("open" if old.get("opened_at") else "unopened")
    line += (f"{state}, spent ${float(old.get('spent_usd') or 0):.2f} of "
             f"${float(old.get('allowance_usd') or 0):.2f}")
    await _copy_one(run, n.usage, target, dict(old), counts, write=write)
    return line


async def tag_notes(run: Cutover, cred: Credential, conn: DriveConnection, counts: Counts, *, write: bool) -> None:
    """Step c, notes: `r:{tag}:{item}` next to the old `r:{item}` (removed at --cleanup)."""
    n = run.names
    for doc_id, doc in sorted((await run.docs_of(n.notes, cred.user_id, ("match_keys",))).items()):
        keys = list(doc.get("match_keys") or ())
        item_id = note_item_id(keys, cred.drive_id, run.id_space)
        if not item_id:
            continue
        tagged = tagged_ref_key(conn.ref_tag, item_id)
        if tagged in keys:
            counts.add(f"already tagged {n.notes}")
            continue
        counts.add(f"tagged {n.notes}")
        if write:
            await run.col(n.notes).document(doc_id).update({"match_keys": ArrayUnion([tagged])})


async def apply_user(run: Cutover, user_id: str, cred: Optional[Credential], *, write: bool) -> Tuple[Counts, str]:
    counts = Counts()
    line = ""
    conn = await ensure_connection(run, cred, counts, write=write) if cred is not None else None
    await purge_old_index(run, user_id, counts, write=write)
    if cred is not None and conn is not None:
        await copy_transcripts(run, cred, counts, write=write)
        await copy_mirror(run, cred, counts, write=write)
        line = await copy_initial_build(run, cred, counts, write=write)
        await tag_notes(run, cred, conn, counts, write=write)
    else:
        line = "no OneDrive credential: the old index is purged, nothing is copied"
    return counts, line


# -- rollback and cleanup -----------------------------------------------------------------------------------------

async def stamp_old_heads(run: Cutover, user_id: str, counts: Counts, *, write: bool) -> None:
    """E22's guard on the rollback side: every old-key transcript head gets `last_used_at` = now, so the old
    revision's orphan sweep (30 days by `last_used_at`, before its rebuilt records carry the keys again) cannot delete a
    paid transcript before the rebuild re-attaches it. Both rollback modes run it."""
    n = run.names
    heads = await run.docs_of(n.transcripts, user_id, ("content_key",))
    old = sorted(i for i, d in heads.items()
                 if parse_legacy_content_key(str(d.get("content_key") or ""), run.id_space) is not None)
    counts.add(f"stamped {n.transcripts}", len(old))
    if write and old:
        await run._commit([("update", run.col(n.transcripts).document(i), {"last_used_at": run.now}) for i in old])


async def purge_new_user(run: Cutover, user_id: str, *, write: bool) -> Counts:
    """--purge-new: the connection-scoped index documents, sync states, connections and the drive settings; the old-key
    heads are stamped (`stamp_old_heads`)."""
    n, counts = run.names, Counts()
    await stamp_old_heads(run, user_id, counts, write=write)
    for conn in await run.connections.list_for_user(user_id):
        for name in n.index_collections():
            ids = await run.connection_index_ids(name, conn.connection_id)
            counts.add(f"deleted {name}", len(ids))
            if write and ids:
                await run.delete_ids(name, ids, n.windows if name == n.jobs else None)
        if await run.get(n.sync_state, sync_state_doc_id(conn.scope)) is not None:
            counts.add(f"deleted {n.sync_state}")
            if write:
                await run.col(n.sync_state).document(sync_state_doc_id(conn.scope)).delete()
        counts.add(f"deleted {n.connections}")
        if write:
            await run.connections.remove(conn)
    if await run.connections.get_settings(user_id) is not None:
        counts.add(f"deleted {n.settings}")
        if write:
            await run.connections.remove_settings(user_id)
    return counts


def _without(data: Mapping[str, Any], *names: str) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if k not in names}


async def copy_back_user(run: Cutover, cred: Credential, *, write: bool) -> Counts:
    """--copy-back: what the new keys hold that the old keys lack or hold differently goes back to the old keys."""
    n, counts, user_id, drive = run.names, Counts(), cred.user_id, cred.drive_id
    for head_id, head in sorted((await run.docs_of(n.transcripts, user_id)).items()):
        old_key = legacy_content_key(str(head.get("content_key") or ""), run.id_space, run.family)
        if old_key is None:
            continue
        old_id = transcript_doc_id(user_id, old_key)
        target = {**head, "content_key": old_key}
        parts = await run.subdocs(n.transcripts, head_id, n.parts)
        old = await run.get(n.transcripts, old_id)
        old_parts = await run.subdocs(n.transcripts, old_id, n.parts) if old is not None else {}
        if old is not None and _without(old, "last_used_at") == _without(target, "last_used_at") and old_parts == parts:
            continue
        counts.add(f"copied back {n.transcripts}")
        counts.add(f"copied back {n.parts}", len(parts))
        if write:
            part_col = run.col(f"{n.transcripts}/{old_id}/{n.parts}")
            writes: List[Tuple[str, Any, Optional[Dict[str, Any]]]] = [
                ("delete", part_col.document(pid), None) for pid in old_parts if pid not in parts]
            writes += [("set", part_col.document(pid), data) for pid, data in sorted(parts.items())]
            await run._commit(writes)
            await run._commit([("set", run.col(n.transcripts).document(old_id), target)])
    identity = onedrive_drive_identity(drive, run.id_space)
    ledger = {i: d for i, d in (await run.docs_of(n.ledger, user_id)).items() if d.get("drive_identity") == identity}
    root = await run.get(n.roots, mirror_root_doc_id(user_id, identity)) or {}
    archive_ids, _ = rekey_archives(user_id, ledger, str(root.get("root_item_id") or ""), identity, drive)
    for doc_id, doc in sorted(ledger.items()):
        data = _without(doc, "drive_identity", "source_identity")
        data["drive_id"] = drive
        key = str(doc.get("content_key") or "")
        if key:
            data["content_key"] = legacy_content_key(key, run.id_space, run.family) or key
        if doc.get("kind") == KIND_ARCHIVE:
            if doc_id not in archive_ids:
                counts.add(f"skipped {n.ledger} (archive folder not re-keyable)")
                continue
            target = archive_ids[doc_id]
            data["source_item_id"] = target.rsplit(":", 1)[1]
        else:
            item_id = identity_item_id(str(doc.get("source_identity") or ""), drive, run.id_space)
            if not item_id:
                counts.add(f"skipped {n.ledger} (no source identity)")
                continue
            data["source_item_id"] = item_id
            target = legacy_ledger_key(user_id, str(doc.get("kind")), item_id)
        old = await run.get(n.ledger, target)
        if old is not None and _without(old, "drive_identity", "source_identity") == data:
            continue
        counts.add(f"copied back {n.ledger}")
        if write:
            await run.col(n.ledger).document(target).set(data)
    await untag_notes_back(run, cred, counts, write=write)
    await stamp_old_heads(run, user_id, counts, write=write)   # after the heads copied back: they are stamped too
    return counts


async def untag_notes_back(run: Cutover, cred: Credential, counts: Counts, *, write: bool) -> None:
    """--copy-back, notes: a note written after the cut-over carries only `r:{tag}:{item}`; the old revision finds a
    note by `r:{item}`, so that key is added (the tagged one stays for a roll-forward). The item comes from the tagged
    key of one of the user's connections and must give the note's `i:` key on the credential's drive."""
    n, user_id = run.names, cred.user_id
    tags = [c.ref_tag for c in await run.connections.list_for_user(user_id)]
    for doc_id, doc in sorted((await run.docs_of(n.notes, user_id, ("match_keys",))).items()):
        keys = list(doc.get("match_keys") or ())
        binding = {k for k in keys if k.startswith(BINDING_KEY_PREFIX)}
        for key in keys:
            for tag in tags:
                if not key.startswith(f"{REF_KEY_PREFIX}{tag}:"):
                    continue
                item_id = key[len(REF_KEY_PREFIX) + len(tag) + 1:]
                if tagged_ref_key(tag, item_id) != key or legacy_ref_key(item_id) in keys:
                    continue
                identity = onedrive_item_identity(cred.drive_id, item_id, run.id_space)
                if not identity or item_binding_key(identity) not in binding:
                    counts.add(f"skipped {n.notes} (bound to another drive)")
                    continue
                counts.add(f"ref key added back {n.notes}")
                if write:
                    await run.col(n.notes).document(doc_id).update({"match_keys": ArrayUnion([legacy_ref_key(item_id)])})


async def cleanup_user(run: Cutover, cred: Credential, *, write: bool) -> Counts:
    """--cleanup: every old-key document whose new-key twin exists; the old note keys, the credential's drive_id and
    the UserBotConfig drive flags."""
    n, counts, user_id, drive = run.names, Counts(), cred.user_id, cred.drive_id
    identity = onedrive_drive_identity(drive, run.id_space)
    for head_id, head in sorted((await run.docs_of(n.transcripts, user_id)).items()):
        new_key = new_content_key(str(head.get("content_key") or ""), drive, run.id_space)
        if new_key is None:
            continue
        if await run.get(n.transcripts, transcript_doc_id(user_id, new_key)) is None:
            counts.add(f"kept {n.transcripts} (no new-key twin)")
            continue
        counts.add(f"deleted {n.transcripts}")
        if write:
            await run.delete_ids(n.transcripts, [head_id], n.parts)
    roots = {i: d for i, d in (await run.docs_of(n.roots, user_id)).items() if not d.get("drive_identity")}
    ledger = {i: d for i, d in (await run.docs_of(n.ledger, user_id)).items() if not d.get("drive_identity")}
    root = roots.get(legacy_root_doc_id(user_id, drive)) or {}
    archive_ids, _ = rekey_archives(user_id, ledger, str(root.get("root_item_id") or ""), drive, identity)
    for doc_id, doc in sorted(ledger.items()):
        if doc.get("kind") == KIND_ARCHIVE:
            twin = archive_ids.get(doc_id, "")
        else:
            source = onedrive_item_identity(drive, str(doc.get("source_item_id") or ""), run.id_space)
            twin = ledger_key(user_id, str(doc.get("kind")), identity, source) if source else ""
        await _delete_if_twin(run, n.ledger, doc_id, twin, counts, write=write)
    for root_id in sorted(roots):
        await _delete_if_twin(run, n.roots, root_id, mirror_root_doc_id(user_id, identity), counts, write=write)
    await _delete_if_twin(run, n.usage, legacy_initial_doc_id(user_id),
                          initial_build_doc_id(user_id, cred.realm, cred.subject), counts, write=write, absent_ok=True)
    tags = {c.ref_tag for c in await run.connections.list_for_user(user_id) if c.credential_id == cred.credential_id}
    for doc_id, doc in sorted((await run.docs_of(n.notes, user_id, ("match_keys",))).items()):
        keys = list(doc.get("match_keys") or ())
        item_id = note_item_id(keys, drive, run.id_space)
        if item_id and any(tagged_ref_key(tag, item_id) in keys for tag in tags):
            counts.add(f"untagged {n.notes}")
            if write:
                await run.col(n.notes).document(doc_id).update({"match_keys": ArrayRemove([legacy_ref_key(item_id)])})
    credential = await run.get(n.credentials, cred.doc_id)
    if credential is not None and "drive_id" in credential:
        counts.add(f"drive_id removed {n.credentials}")
        if write:
            await run.col(n.credentials).document(cred.doc_id).update({"drive_id": DELETE_FIELD})
    config = ((await run.get(n.users, user_id)) or {}).get("config") or {}
    gone = {f"config.{flag}": DELETE_FIELD for flag in ("drive_indexing_enabled", "drive_transcribe_enabled")
            if flag in config}
    if gone:
        counts.add(f"flags removed {n.users}")
        if write:
            await run.col(n.users).document(user_id).update(gone)
    return counts


async def _delete_if_twin(run: Cutover, name: str, doc_id: str, twin: str, counts: Counts, *, write: bool,
                          absent_ok: bool = False) -> None:
    if await run.get(name, doc_id) is None:
        if not absent_ok:
            counts.add(f"missing {name}")
        return
    if not twin or await run.get(name, twin) is None:
        counts.add(f"kept {name} (no new-key twin)")
        return
    counts.add(f"deleted {name}")
    if write:
        await run.col(name).document(doc_id).delete()


# -- the driver --------------------------------------------------------------------------------------------------

async def run_cutover(run: Cutover, mode: str, *, write: bool,
                      only_users: Sequence[str] = ()) -> Tuple[int, List[str]]:
    """Returns (exit code, output lines). `mode` in MODES; `write` False counts only (the dry run of a mode)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    write = write and mode != "dry-run"
    creds = await run.credentials()
    users = sorted(set(creds) | set(await run.old_state_users()))
    if only_users:
        users = [u for u in users if u in set(only_users)]
    lines: List[str] = [f"mode {mode}{'' if write else ' (dry run: nothing written)'}; users {len(users)}"]
    if mode in ("dry-run", "apply"):
        problems = await infra_problems(run, indexes=True)
        for user_id in users:
            problems += await user_problems(run, user_id, creds.get(user_id, []))
    else:
        problems = await infra_problems(run, indexes=False)
        if mode == "purge-new":   # the drain rule: never delete in-flight jobs or batches (they bill when they finish)
            for user_id in users:
                problems += await in_flight_problems(run, user_id)
    lines += [f"REFUSED: {p}" for p in problems]
    if problems and write:
        lines.append("nothing written: fix the above and re-run")
        return 1, lines
    total = Counts()
    for user_id in users:
        cred = (creds.get(user_id) or [None])[0]
        if mode in ("dry-run", "apply"):
            counts, extra = await apply_user(run, user_id, cred, write=write)
        elif mode == "purge-new":
            counts, extra = await purge_new_user(run, user_id, write=write), ""
        elif cred is None and mode == "copy-back":
            counts, extra = Counts(), "no OneDrive credential: old-key heads stamped only"
            await stamp_old_heads(run, user_id, counts, write=write)
        elif cred is None:
            counts, extra = Counts(), "no OneDrive credential: nothing to do"
        elif mode == "copy-back":
            counts, extra = await copy_back_user(run, cred, write=write), ""
        else:
            counts, extra = await cleanup_user(run, cred, write=write), ""
        total.merge(counts)
        lines.append(f"{user_id[:8]}: {counts.line()}" + (f"; {extra}" if extra else ""))
    lines.append(f"total: {total.line()}")
    return (1 if problems else 0), lines


# -- production infra (gcloud, read-only) -------------------------------------------------------------------------

def _index_shape(index: Mapping[str, Any]) -> Tuple[str, str, Tuple[Tuple[str, str], ...]]:
    """(collection group, query scope, fields) with the trailing `__name__` Firestore adds dropped."""
    group = str(index.get("collectionGroup") or str(index.get("name", "")).split("/collectionGroups/")[-1].split("/")[0])
    fields: List[Tuple[str, str]] = []
    for f in index.get("fields") or ():
        mode = f.get("order") or f.get("arrayConfig") or ("VECTOR" if "vectorConfig" in f else "")
        fields.append((str(f.get("fieldPath")), str(mode)))
    while fields and fields[-1][0] == "__name__":
        fields.pop()
    return group, str(index.get("queryScope") or "COLLECTION"), tuple(fields)


def missing_from(required: Sequence[Mapping[str, Any]], listed: Sequence[Mapping[str, Any]]) -> List[str]:
    """Required index shapes that no READY listed index has."""
    ready = {_index_shape(i) for i in listed if i.get("state") == "READY"}
    out = []
    for index in required:
        shape = _index_shape(index)
        if shape not in ready:
            out.append(f"{shape[0]} ({', '.join(f for f, _ in shape[2])})")
    return out


def required_indexes(path: Path) -> List[Dict[str, Any]]:
    """The indexes this slice adds: every composite of config/firestore.indexes.json on `connection_id` or
    `drive_identity`."""
    return [i for i in json.loads(path.read_text(encoding="utf-8"))
            if any(f.get("fieldPath") in ("connection_id", "drive_identity") for f in i.get("fields") or ())]


@dataclass
class GcloudInfra:
    project: str
    location: str
    queue_name: str
    job_names: Sequence[str]
    database: str

    async def _json(self, *args: str) -> Any:
        proc = await asyncio.to_thread(subprocess.run, ["gcloud", *args, f"--project={self.project}", "--format=json"],
                                       capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"gcloud {' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
        return json.loads(proc.stdout or "null")

    async def queue(self) -> Tuple[bool, int]:
        queue = await self._json("tasks", "queues", "describe", self.queue_name, f"--location={self.location}")
        tasks = await self._json("tasks", "list", f"--queue={self.queue_name}", f"--location={self.location}")
        return (queue or {}).get("state") == "PAUSED", len(tasks or [])

    async def jobs_paused(self) -> Dict[str, bool]:
        out = {}
        for job in self.job_names:
            data = await self._json("scheduler", "jobs", "describe", job, f"--location={self.location}")
            out[job] = (data or {}).get("state") == "PAUSED"
        return out

    async def missing_indexes(self, required: Sequence[Mapping[str, Any]]) -> List[str]:
        listed = await self._json("firestore", "indexes", "composite", "list", f"--database={self.database}")
        return missing_from(required, listed or [])


async def _main(args: argparse.Namespace) -> int:
    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=args.project or None, database=env.firestore_database_id)
    names = Names.of(env)
    repo = FirestoreDriveConnectionRepository(db, names.connections, names.settings)
    infra = GcloudInfra(project=args.project, location=args.location, queue_name=args.queue, job_names=args.job,
                        database=env.firestore_database_id)
    run = Cutover(db=db, names=names, connections=RepositoryConnectionStore(repo, db, names), infra=infra,
                  now=datetime.now(timezone.utc),
                  required_indexes=required_indexes(Path(__file__).resolve().parents[2] / "config" /
                                                    "firestore.indexes.json"))
    mode = next((m for m in ("apply", "purge-new", "copy-back", "cleanup") if getattr(args, m.replace("-", "_"))),
                "dry-run")
    write = mode != "dry-run" and not args.dry_run
    code, lines = await run_cutover(run, mode, write=write, only_users=args.user)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="steps a-d of RFC §12 (default: dry run)")
    modes.add_argument("--purge-new", action="store_true", help="rollback: delete the connection-scoped documents")
    modes.add_argument("--copy-back", action="store_true", help="rollback after step 5: new keys back to old keys")
    modes.add_argument("--cleanup", action="store_true", help="after acceptance: delete the old-key documents")
    parser.add_argument("--dry-run", action="store_true", help="count only (the default without a mode)")
    parser.add_argument("--user", action="append", default=[], help="limit to these users (repeatable)")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--queue", default="drive-sync-dev")
    parser.add_argument("--job", action="append", default=None,
                        help="a Scheduler job that must be paused (default: start_drive_sync's and "
                             "reconnect_notice_sweep's)")
    parsed = parser.parse_args()
    if parsed.job is None:
        parsed.job = ["alek-bot-dev-start-drive-sync", "alek-bot-dev-reconnect-notice-sweep"]
    if not parsed.project:
        parser.error("--project or GOOGLE_CLOUD_PROJECT is required")
    sys.exit(asyncio.run(_main(parsed)))
