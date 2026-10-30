"""
Backfill `integration` and `account_id` on stored credentials (MULTI_DRIVE_RFC §4.1; slice 1a).

`FirestoreCredentialStore` writes both plaintext fields on every save; a record saved before the 1a deploy has neither.
This owner script sets them:
  - `integration` from the connector registry (`get(kind).integration`);
  - `account_id` from the owner's user profile document (`{prefix}domain_users_v2/{user_id}`, field `account_id`).
An unknown kind or a user without a profile/account_id is counted and listed by truncated id, never guessed. It writes
only those two plaintext fields with `update` (never `set`, never the `secret` field). Idempotent: a second run sets
nothing. Dry run by default; `--apply` writes. Output carries counts and ids truncated to 8 characters only.

Usage (project and credentials from the environment / ADC, collections from APP_ENV):
    python scripts/infrastructure/credentials_integration_backfill.py [--apply]
Exit code: 0 always except an unexpected error.
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.composition.connector_registry import ConnectorRegistry  # noqa: E402

_SHORT = 8


def _short(value: Any) -> str:
    return str(value)[:_SHORT]


async def backfill(credentials_col: Any, users_col: Any, *, apply: bool,
                   registry: Optional[ConnectorRegistry] = None) -> Dict[str, Any]:
    """Scan the credential collection; return the counts and the truncated ids of the unresolved records."""
    registry = registry or ConnectorRegistry()
    report: Dict[str, Any] = {
        "scanned": 0, "complete": 0, "integration_set": 0, "account_id_set": 0,
        "unknown_kind": 0, "no_profile": 0, "unknown_kind_ids": [], "no_profile_users": [],
    }
    accounts: Dict[str, Optional[str]] = {}   # user_id -> account_id (None: no profile / no account_id)

    async def account_of(user_id: str) -> Optional[str]:
        if user_id not in accounts:
            snap = await users_col.document(user_id).get()
            value = ((snap.to_dict() or {}).get("account_id") if snap.exists else None)
            accounts[user_id] = value if isinstance(value, str) and value.strip() else None
        return accounts[user_id]

    no_profile_users: List[str] = []
    async for snap in credentials_col.stream():
        data = snap.to_dict() or {}
        report["scanned"] += 1
        fields: Dict[str, Any] = {}

        if not data.get("integration"):
            try:
                fields["integration"] = registry.get(str(data.get("kind"))).integration.value
            except KeyError:
                report["unknown_kind"] += 1
                report["unknown_kind_ids"].append(_short(snap.id))
        if not data.get("account_id"):
            user_id = str(data.get("user_id") or "")
            account_id = await account_of(user_id) if user_id else None
            if account_id is None:
                report["no_profile"] += 1
                if _short(user_id) not in no_profile_users:
                    no_profile_users.append(_short(user_id))
            else:
                fields["account_id"] = account_id

        if not fields and data.get("integration") and data.get("account_id"):
            report["complete"] += 1
        if "integration" in fields:
            report["integration_set"] += 1
        if "account_id" in fields:
            report["account_id_set"] += 1
        if fields and apply:
            await credentials_col.document(snap.id).update(fields)

    report["no_profile_users"] = no_profile_users
    return report


def format_report(report: Dict[str, Any], *, apply: bool) -> str:
    verb = "set" if apply else "would set"
    lines = [
        f"{'APPLY' if apply else 'DRY RUN'}: scanned {report['scanned']}, already complete {report['complete']}",
        f"{verb} integration: {report['integration_set']}",
        f"{verb} account_id: {report['account_id_set']}",
        f"unknown kind: {report['unknown_kind']} {report['unknown_kind_ids']}",
        f"no profile / no account_id: {report['no_profile']} (users {report['no_profile_users']})",
    ]
    if not apply:
        lines.append("nothing written; re-run with --apply")
    return "\n".join(lines)


async def _main(apply: bool) -> int:
    from scripts._firestore import async_client
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = async_client()
    report = await backfill(db.collection(env.credentials_collection), db.collection(env.domain_users_collection),
                            apply=apply)
    print(format_report(report, apply=apply))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    sys.exit(asyncio.run(_main(parser.parse_args().apply)))
