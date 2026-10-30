"""
Re-buy transcripts after a `TRANSCRIBE_VERSION` bump (USER_DRIVE_INDEXING_RFC §27.13, Δ122). A bump redoes NOTHING by
itself: a prompt or model tweak must never re-bill hours of audio. This script is the only way to re-buy: it lists what a
requeue would redo and what that would cost, and with `--apply` resets exactly those jobs.

A candidate is a media record whose transcript is `current`, was made under an OLDER `TRANSCRIBE_VERSION`, and is a real
transcript or the model's `no_speech` verdict. Stubs decided from metadata or the probe (`too_large`, `too_long`,
`no_audio`) cost nothing to redo and are left alone, and so are failed files. Copies share one stored transcript (one
content key), so one purchase redoes all of them and is priced once; a key that a NON-candidate record also uses (a copy
already made under the current version) is skipped whole, because deleting its stored transcript would break that record.

The estimate is the stage's own reservation (`file_estimate_usd`: the held hourly price over every window, overlaps
included, plus each window's prompt allowance), an upper bound of what the batch will bill. The monthly cap still applies:
what does not fit is parked for the next month by the stage as for any file.

Per connection (MULTI_DRIVE_RFC §8): the candidates are one connection's records (`--connection`, or the user's one live
connection); the stored transcripts are the user's (§10), so a key that a record of the user's OTHER connections uses is
skipped like a key a non-candidate copy uses. The "transcription is off" warning reads the connection's switch.

`--apply` takes the connection's lease (refused while a tick runs: retry in a few minutes) and, per candidate content key:
sets the records `ready` (stub, attempts, holds and wake-up cleared), THEN deletes the stored transcript, so a crash in
between leaves records the stage finishes for free from the store; a re-run finishes the delete. The next tick buys the audio
again. Nothing is logged but counts.

Dry run by default: prints counts and the estimate, writes nothing.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_transcribe_requeue.py --user <user_id> [--connection <id>] [--apply]
Exit code: 0 (also for a dry run or nothing to redo), 1 when nothing is stored for the connection or the lease is busy.
"""
import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.infrastructure.drive_connection_arg import resolve_connection  # noqa: E402
from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.drive_index import TRANSCRIBE_VERSION, DriveIndexRecord  # noqa: E402
from src.domain.drive_index_params import TRANSCRIBE_MAX_SECONDS, TRANSCRIBE_USD_PER_MONTH  # noqa: E402
from src.domain.drive_index_policy import TRANSCRIBED_KINDS  # noqa: E402
from src.domain.drive_index_planner import month_key  # noqa: E402
from src.domain.drive_transcript_plan import (  # noqa: E402
    CURRENT, HELD, PARKED, READY, WAITING, WORKING, effective_monthly_cap, file_estimate_usd,
)
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

_HOLD = timedelta(minutes=10)    # the lease is held for the writes of one run
_PAGE = 500
_WRITE_BATCH = 100
_REDONE_STUBS = ("", "no_speech")


@dataclass
class Plan:
    """What a requeue would do: candidate records grouped by content key; keys skipped because a record outside the set
    shares them."""
    groups: Dict[str, List[DriveIndexRecord]] = field(default_factory=dict)
    skipped_shared: int = 0
    estimate_usd: float = 0.0

    @property
    def files(self) -> int:
        return sum(len(records) for records in self.groups.values())


def is_candidate(rec: DriveIndexRecord) -> bool:
    return (not rec.is_folder and rec.kind in TRANSCRIBED_KINDS and rec.transcript_state == CURRENT
            and rec.transcript_key != "" and 0 < rec.transcript_version < TRANSCRIBE_VERSION
            and rec.transcript_stub in _REDONE_STUBS)


def _seconds(rec: DriveIndexRecord) -> float:
    return float(min(rec.transcript_seconds, TRANSCRIBE_MAX_SECONDS)) if rec.transcript_seconds > 0 else 0.0


def build_plan(records: List[DriveIndexRecord], others: Sequence[DriveIndexRecord] = ()) -> Plan:
    """Group candidates by key; drop a key that any non-candidate record of the same key uses (its store must stay),
    and any key a record of another connection of the user uses (`others`: the store is the user's, §10)."""
    candidates: Dict[str, List[DriveIndexRecord]] = {}
    blocked = {rec.transcript_key for rec in others if rec.transcript_key}
    for rec in records:
        if rec.is_folder or not rec.transcript_key:
            continue
        if is_candidate(rec):
            candidates.setdefault(rec.transcript_key, []).append(rec)
        elif rec.transcript_state in (CURRENT, WAITING, WORKING, READY, HELD, PARKED):
            blocked.add(rec.transcript_key)
    plan = Plan()
    for key, recs in sorted(candidates.items()):
        if key in blocked:
            plan.skipped_shared += len(recs)
            continue
        plan.groups[key] = sorted(recs, key=lambda r: r.item_id)
        # one purchase per key: the longest copy's duration prices it
        plan.estimate_usd += file_estimate_usd(max(_seconds(r) for r in recs))
    return plan


def summary(plan: Plan) -> str:
    skipped = f"; skipped, shared with a record that stays: {plan.skipped_shared}" if plan.skipped_shared else ""
    return (f"{plan.files} files in {len(plan.groups)} transcripts (TRANSCRIBE_VERSION {TRANSCRIBE_VERSION}) would be "
            f"re-bought; estimated cost up to ${plan.estimate_usd:,.2f} (held price, an upper bound){skipped}")


async def _media_records(repo: DriveIndexRepository, scope: ConnectionScope) -> List[DriveIndexRecord]:
    records: List[DriveIndexRecord] = []
    after: Optional[str] = None
    while True:
        page = await repo.records_page(scope, after, _PAGE)
        records.extend(r for r in page if not r.is_folder and r.kind in TRANSCRIBED_KINDS)
        if len(page) < _PAGE:
            return records
        after = page[-1].item_id


async def load_plan(repo: DriveIndexRepository, scope: ConnectionScope,
                    others: Sequence[ConnectionScope] = ()) -> Plan:
    """The plan of one connection; `others` are the user's other connections, whose records only block keys."""
    shared: List[DriveIndexRecord] = []
    for other in others:
        if other != scope:
            shared += await _media_records(repo, other)
    return build_plan(await _media_records(repo, scope), shared)


def reset(rec: DriveIndexRecord) -> DriveIndexRecord:
    """The record goes back to the queue; the stage buys the key again, since the stored transcript is deleted."""
    return replace(rec, transcript_state=READY, transcript_stub="", transcript_attempts=0, transcript_hold=None,
                   transcript_hold_version="", transcript_wake_at=None, model_read_at=None)


async def headroom_note(repo: DriveIndexRepository, user_id: str, plan: Plan, now: datetime, cap_usd: float,
                        transcribe_enabled: Optional[bool]) -> str:
    """The month's headroom (cap - spent - reserved; the cap is the user's, shared by the connections) beside the
    estimate, with a warning when the estimate exceeds it or the connection's transcription switch is off
    (`transcribe_enabled` None = unknown, no warning about it)."""
    usage = await repo.get_model_usage(user_id, month_key(now))
    room = max(0.0, cap_usd - usage.cap_usd("transcribe") - max(0.0, usage.transcribe_reserved_usd))
    note = f"; this month's headroom ${room:,.2f} (cap ${cap_usd:,.2f} less spent and reserved)"
    if plan.estimate_usd > room:
        note += " — WARNING: the estimate exceeds it, the stage parks what does not fit until next month"
    if transcribe_enabled is False:
        note += (" — WARNING: transcription (transcribe_enabled) is OFF for this connection, nothing will be bought "
                 "until it is on")
    return note


async def requeue(repo: DriveIndexRepository, scope: ConnectionScope, *, apply: bool, now: datetime, owner: str,
                  cap_usd: float = TRANSCRIBE_USD_PER_MONTH, transcribe_enabled: Optional[bool] = None,
                  others: Sequence[ConnectionScope] = ()) -> Tuple[int, str]:
    """Returns (exit code, message). The estimate and the month's headroom are in the message either way, before
    anything is written. `others`: the user's other connections (their records keep a shared key)."""
    if await repo.get_sync_state(scope) is None:
        return 1, "no sync state: nothing is indexed for this connection"
    plan = await load_plan(repo, scope, others)
    if not plan.groups:
        return 0, "nothing to redo" + (f" ({plan.skipped_shared} files skipped, shared)" if plan.skipped_shared else "")
    note = await headroom_note(repo, scope.user_id, plan, now, cap_usd, transcribe_enabled)
    if not apply:
        return 0, f"dry run: {summary(plan)}{note}; re-run with --apply"
    if await repo.acquire_lease(scope, owner, now, now + _HOLD, create=False) is None:
        return 1, "a tick holds the lease (or the state just went away); retry in a few minutes"
    try:
        records = [r for recs in plan.groups.values() for r in recs]
        for start in range(0, len(records), _WRITE_BATCH):
            await repo.upsert_records([reset(r) for r in records[start:start + _WRITE_BATCH]])
        for key in plan.groups:    # after the records: a crash in between costs nothing (the store still answers)
            await repo.delete_transcript(scope.user_id, key)
    finally:
        await repo.release_lease(scope, owner)
    return 0, f"requeued: {summary(plan)}{note}"


def resolve_cap(cli: Optional[float], env: Optional[str]) -> Tuple[float, str]:
    """The monthly cap this run assumes and where it came from: `--cap-usd`, else the deploy-only
    DRIVE_TRANSCRIBE_USD_CAP_DEV (what the live indexer was deployed with), else the $20 maximum. The local
    DRIVE_TRANSCRIBE_USD_CAP is not read: it is not what the live service applies."""
    if cli is not None:
        return effective_monthly_cap(str(cli)), "--cap-usd"
    if (env or "").strip():
        return effective_monthly_cap(env), "DRIVE_TRANSCRIBE_USD_CAP_DEV"
    return effective_monthly_cap(None), "default (no cap knob)"


async def _main(user_id: str, connection_id: Optional[str], apply: bool, cap_usd: Optional[float] = None) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    connections = FirestoreDriveConnectionRepository(db, env.drive_connections_collection,
                                                     env.drive_user_settings_collection)
    conn, why = await resolve_connection(connections, user_id, connection_id)
    if conn is None:
        print(f"refused: {why}")
        return 1
    others = [c.scope for c in await connections.list_for_user(user_id) if c.connection_id != conn.connection_id]
    cap, source = resolve_cap(cap_usd, os.getenv("DRIVE_TRANSCRIBE_USD_CAP_DEV"))
    print(f"monthly cap assumed: ${cap:.2f} (from {source}; make sure it is the live one)")
    code, message = await requeue(FirestoreDriveIndexRepository(db, env), conn.scope, apply=apply,
                                  now=datetime.now(timezone.utc), owner=f"requeue-{uuid.uuid4().hex}",
                                  cap_usd=cap, transcribe_enabled=conn.transcribe_enabled, others=others)
    print(message)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose transcripts are re-bought")
    parser.add_argument("--connection", default=None, help="the connection (default: the user's one live connection)")
    parser.add_argument("--apply", action="store_true", help="reset the jobs (default: dry run, estimate only)")
    parser.add_argument("--cap-usd", type=float, default=None,
                        help="the live monthly cap to assume (default: DRIVE_TRANSCRIBE_USD_CAP_DEV from .env, else $20)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.connection, args.apply, args.cap_usd)))
