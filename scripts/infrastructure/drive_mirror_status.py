"""
Read-only status of the transcript mirror (USER_DRIVE_INDEXING_RFC §27.9, T2), per user. It writes nothing to Firestore and
nothing to a drive; its output goes ONLY to `scripts/memory/` (gitignored: Δ67's rule, the file holds ids and cursors of a
real user), and stdout gets just the path and a one-line count.

For each user it reports:
  - the ledger documents per kind and state (file / folder / archive x active / archived / detached);
  - the pinned store keys (the transcripts the ledger keeps alive across a purge) and how many documents pin them;
  - the deletion marks waiting (`gone_pending`), how many are ripe and how many are not ripe yet (`mark_ready_at` in the future: a grace hour, or a hold or charge back-off);
  - the parked documents (`failed_at` set: tried again only after MIRROR_FAILED_RETRY) and the ones backing off (`retry_at`);
  - the detached documents due for the weekly recheck;
  - per root document (one per drive, keyed by its drive identity since MULTI_DRIVE_RFC §10): the root's item id, the cursors (seed, scan, audit folder and page), the clocks
    (`created_at`, `audited_at`, whether the audit is due), the holds (`quota_hold_until`, `readme_blocked_at`,
    `readme_pending_at`), the loss marks (`root_gone_at`, a reset under way = the root id among `previous_root_ids`) and
    `orphans_reported`.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_mirror_status.py --user <user_id> [--user <user_id> ...]
    python scripts/infrastructure/drive_mirror_status.py --all-users
`--all-users` lists only users that still have a sync state on some connection. After the slice-1b cut-over the owner
reads it for I5/I6 (RFC §12 step 6): no new `_Transcripts` root (one root per drive identity), no archive moves. An indexing opt-out deletes the sync state and keeps the mirror
(OQ-T10), so such a user's leftover mirror is seen only when named with `--user`.
Exit code: 0 when written, 1 when a store read failed (a missing composite index is named in the output).
"""
import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.drive_index import DriveIndexStoreUnavailable  # noqa: E402
from src.domain.drive_index_params import MIRROR_AUDIT_DAYS  # noqa: E402
from src.domain.drive_mirror import MIRROR_DETACHED, MirrorDoc, MirrorRoot  # noqa: E402
from src.domain.drive_mirror_plan import audit_due  # noqa: E402
from src.ports.drive_mirror_repository import DriveMirrorRepository  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "memory"
_PAGE = 200
_RECHECK = timedelta(days=MIRROR_AUDIT_DAYS)   # the detached documents' weekly recheck (§27.9.9)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


async def _all_docs(mirror_repo: DriveMirrorRepository, user_id: str, drive_identity: str) -> List[MirrorDoc]:
    docs: List[MirrorDoc] = []
    for reader in (mirror_repo.folder_docs, mirror_repo.scan_page):
        after: Optional[str] = None
        while True:
            page = await reader(user_id, drive_identity, after, _PAGE)
            docs.extend(page)
            if len(page) < _PAGE:
                break
            after = page[-1].key
    return docs


def root_report(root: MirrorRoot, now: datetime) -> Dict[str, Any]:
    """One root document's cursors, clocks, holds and loss marks (no names)."""
    return {
        "drive_identity": root.drive_identity,
        "root_item_id": root.root_item_id,
        "previous_roots": len(root.previous_root_ids),
        "ledger_reset_under_way": root.root_item_id in root.previous_root_ids,
        "cursors": {"seed_version": root.seed_version, "seed": root.seed_cursor, "scan": root.scan_cursor,
                    "audit_folder": root.audit_folder_key, "audit_page": root.audit_page_cursor},
        "clocks": {"created_at": _iso(root.created_at), "audited_at": _iso(root.audited_at),
                   "audit_due": audit_due(root, now)},
        "holds": {"quota_hold_until": _iso(root.quota_hold_until),
                  "quota_hold_active": bool(root.quota_hold_until and root.quota_hold_until > now),
                  "readme_blocked_at": _iso(root.readme_blocked_at), "readme_pending_at": _iso(root.readme_pending_at)},
        "loss": {"root_gone_at": _iso(root.root_gone_at)},
        "orphans_reported": root.orphans_reported,
        "readme_saved": bool(root.readme_item_id),
    }


def docs_report(docs: Sequence[MirrorDoc], now: datetime) -> Dict[str, Any]:
    """Counts only: per kind and state, the marks, the parked and backing-off documents, the detached due."""
    per = Counter(f"{d.kind}/{d.state}" for d in docs)
    marks = [d for d in docs if d.gone_pending]
    return {
        "documents": len(docs),
        "per_kind_state": dict(sorted(per.items())),
        "marks_waiting": len(marks),
        "marks_ripe": sum(1 for d in marks if d.mark_ready_at is not None and d.mark_ready_at <= now),
        "marks_not_ripe": sum(1 for d in marks if d.mark_ready_at is not None and d.mark_ready_at > now),
        "parked": sum(1 for d in docs if d.failed_at is not None),
        "backing_off": sum(1 for d in docs if d.failed_at is None and d.retry_at is not None and d.retry_at > now),
        "detached_due": sum(1 for d in docs if d.state == MIRROR_DETACHED
                            and (d.checked_at is None or d.checked_at < now - _RECHECK)),
    }


async def user_status(mirror_repo: DriveMirrorRepository, user_id: str, now: datetime) -> Dict[str, Any]:
    roots = await mirror_repo.roots(user_id)
    out: Dict[str, Any] = {"user_id": user_id, "roots": [], "pinned_keys": sorted(await mirror_repo.pinned_keys(user_id))}
    out["pinned_key_count"] = len(out["pinned_keys"])
    for root in roots:
        docs = await _all_docs(mirror_repo, user_id, root.drive_identity)
        entry = root_report(root, now)
        entry["ledger"] = docs_report(docs, now)
        out["roots"].append(entry)
    return out


async def run(mirror_repo: DriveMirrorRepository, user_ids: Sequence[str], now: datetime,
              out_dir: Path = OUT_DIR) -> Tuple[int, Path]:
    """Writes `drive_mirror_status_<UTC stamp>.json` into `out_dir` (default `scripts/memory/`) and returns (exit code,
    path). A user whose read fails (a store error, a missing index) is recorded as an error entry and sets the code 1."""
    report: Dict[str, Any] = {"generated_at": now.isoformat(), "users": []}
    code = 0
    for user_id in user_ids:
        try:
            report["users"].append(await user_status(mirror_repo, user_id, now))
        except DriveIndexStoreUnavailable as exc:
            code = 1
            report["users"].append({"user_id": user_id, "error": f"store unavailable (a composite index not READY?): {exc}"})
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"drive_mirror_status_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return code, path


async def _main(users: List[str], all_users: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    mirror_repo = FirestoreDriveMirrorRepository(db, env)
    synced = {scope.user_id for scope in await FirestoreDriveIndexRepository(db, env).list_sync_scopes()} \
        if all_users else set()
    names = sorted(set(users) | synced)
    code, path = await run(mirror_repo, names, datetime.now(timezone.utc))
    print(f"wrote {path} ({len(names)} users)")
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", action="append", default=[], help="a user to report (repeatable)")
    parser.add_argument("--all-users", action="store_true", help="every user that has a sync state")
    args = parser.parse_args()
    if not (args.user or args.all_users):
        parser.error("name a user with --user or pass --all-users")
    sys.exit(asyncio.run(_main(args.user, args.all_users)))
