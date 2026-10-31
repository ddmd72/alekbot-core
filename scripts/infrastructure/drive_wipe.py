"""
Wipe every drive document of ONE user (MULTI_DRIVE_RFC slice 1b deploy, owner decision: no migration, no rollback): the
drive is then reconnected in the Cabinet and rebuilt from zero.

Deleted, per collection (every document whose `user_id` is the user, subcollections first): index records, chunks,
hints, transcript jobs (with their window results), transcript batches, sync states (the old per-user one and any
connection's), stored transcripts (with their parts), mirror roots, the mirror ledger, notes (with their versions), the
initial-build record(s) (`{user}:initial…` in the usage collection), drive connections and the user's drive settings.
Kept: the monthly model usage (the money already spent), the credentials (the reconnect reuses or replaces them), the
short-lived note erase requests (they expire by TTL). The drive itself is never touched: delete or move `_Transcripts`
out of the App Folder before the resume, or the rebuilt index takes it for ordinary files.

Run it only with the drive queue and the `start_drive_sync` Scheduler job paused (a tick would write while it deletes).

Dry run by default: prints the counts per collection and writes nothing. `--apply` deletes and prints the counts; a
re-run finds nothing.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_wipe.py --user <user_id> [--apply]
Exit code: 0.
"""
import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.drive_note import VERSIONS_SUBCOLLECTION  # noqa: E402
from src.domain.drive_transcript import safe_doc_id  # noqa: E402
from src.domain.drive_usage import INITIAL_DOC_SUFFIX  # noqa: E402

_WRITE_BATCH = 400     # deletes per batch commit (Firestore takes 500)


@dataclass(frozen=True)
class Names:
    """Collection names (env-prefixed in production, `EnvironmentConfig`)."""
    records: str
    chunks: str
    hints: str
    jobs: str
    windows: str          # the jobs' subcollection
    batches: str
    sync_state: str
    transcripts: str
    parts: str            # the stored transcripts' subcollection
    roots: str
    ledger: str
    notes: str
    usage: str
    connections: str
    settings: str

    @classmethod
    def of(cls, env: Any) -> "Names":
        return cls(records=env.drive_index_records_collection, chunks=env.drive_index_chunks_collection,
                   hints=env.drive_index_hints_collection, jobs=env.drive_transcript_jobs_collection,
                   windows=env.drive_transcript_windows_subcollection,
                   batches=env.drive_transcript_batches_collection, sync_state=env.drive_sync_state_collection,
                   transcripts=env.drive_transcripts_collection, parts=env.drive_transcript_parts_subcollection,
                   roots=env.drive_mirror_roots_collection, ledger=env.drive_transcript_mirror_collection,
                   notes=env.drive_notes_collection, usage=env.drive_model_usage_collection,
                   connections=env.drive_connections_collection, settings=env.drive_user_settings_collection)

    def by_user(self) -> List[Tuple[str, Optional[str]]]:
        """(collection, its subcollection) of every collection wiped by the `user_id` field, children first."""
        return [(self.jobs, self.windows), (self.batches, None), (self.hints, None), (self.chunks, None),
                (self.records, None), (self.sync_state, None), (self.transcripts, self.parts), (self.ledger, None),
                (self.roots, None), (self.notes, VERSIONS_SUBCOLLECTION), (self.connections, None)]


async def _ids(db: Any, name: str, user_id: str) -> List[str]:
    from google.cloud.firestore import FieldFilter

    query = db.collection(name).where(filter=FieldFilter("user_id", "==", user_id)).select(["user_id"])
    return sorted([snap.id async for snap in query.stream()])


async def _delete(db: Any, refs: List[Any]) -> None:
    for start in range(0, len(refs), _WRITE_BATCH):
        batch = db.batch()
        for ref in refs[start:start + _WRITE_BATCH]:
            batch.delete(ref)
        await batch.commit()


async def wipe(db: Any, names: Names, user_id: str, *, apply: bool) -> Dict[str, int]:
    """Counts per collection (subcollections under `parent/sub`); deletes them with `apply`."""
    if not user_id:
        raise ValueError("a user id is required")
    counts: Dict[str, int] = {}
    for name, sub in names.by_user():
        ids = await _ids(db, name, user_id)
        refs: List[Any] = []
        if sub is not None:
            for doc_id in ids:
                children = [snap.reference async for snap in db.collection(f"{name}/{doc_id}/{sub}").stream()]
                counts[f"{name}/{sub}"] = counts.get(f"{name}/{sub}", 0) + len(children)
                refs += children
        counts[name] = len(ids)
        refs += [db.collection(name).document(doc_id) for doc_id in ids]
        if apply:
            await _delete(db, refs)
    initial = safe_doc_id(f"{user_id}:{INITIAL_DOC_SUFFIX}")
    builds = [i for i in await _ids(db, names.usage, user_id) if i.startswith(initial)]
    counts[f"{names.usage} (initial build)"] = len(builds)
    settings = await db.collection(names.settings).document(user_id).get()
    counts[names.settings] = 1 if settings.exists else 0
    if apply:
        await _delete(db, [db.collection(names.usage).document(i) for i in builds])
        if settings.exists:
            await db.collection(names.settings).document(user_id).delete()
    return counts


def report(counts: Dict[str, int], *, apply: bool) -> List[str]:
    verb = "deleted" if apply else "would delete (dry run, re-run with --apply)"
    lines = [f"{verb}: {name} {n}" for name, n in sorted(counts.items()) if n]
    return lines or ["nothing stored for this user"]


async def _main(user_id: str, apply: bool) -> int:
    from google.cloud import firestore

    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    for line in report(await wipe(db, Names.of(env), user_id, apply=apply), apply=apply):
        print(line)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose drive data is wiped")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run, counts only)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.apply)))
