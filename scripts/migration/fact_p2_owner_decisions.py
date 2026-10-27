#!/usr/bin/env python3
"""
Facts P2: write the owner's own decisions on closed facts (his re-review: he decided every fact himself).

Input `--decisions PATH` (a file under scripts/memory/): a JSON list of
    {"fact_id", "account_id", "action", "text"?, "successor_id"?}
with `action` one of
    history     write `text` as a history version (the typed path of a P2 owner-answered history: `as_history`, expiry
                `none`, archival priority, metadata and context cleared)
    current     write `text` as a current fact, expiry `none`, at its stored priority
    supersede   close the fact in favour of `successor_id`, which must be current or written earlier in the same run
    invalidate  invalidate the fact
    revive      current again, text unchanged, expiry `none`
Each listed fact is `archived` or `invalidated`. Per fact, in file order: it is reopened to `stale` exactly as
`fact_p2_reopen_archives.reopen` does (that function, under the `update_time` of the scan), then the action is written
through the typed `FactUpdatePort` as actor OWNER with one reason, `owner decided on review`
(`fact_review_reasons.owner_decided_reason`; a rewrite for the readers, never a revive).

The whole file is validated before anything is read for writing or written: an unknown action, a missing or too long
text (`written_text_problem`: one line, no `[`, at most MAX_FACT_WORDS words), a duplicate fact, a successor that is
neither current nor written earlier in the run (`current`, `history` and `revive` write their own fact) refuse the file.

Dry run by default (reads only): counts on stdout, and `scripts/memory/fact_p2_owner_decisions_<date>.json` (id, action, old
text, new text, state). `--apply` writes; a fact changed since the scan is skipped and listed; a failure never stops the
run (the type of the error is counted, `..._applied.json` lists ids and outcomes). A fact whose reopen landed but whose
action did not stays `stale` with the reopen entry last: the next run resumes it (`resumed`) without reopening again. A
fact whose last trail entry is already the owner's decision is left alone (`already_decided`), so a second `--apply`
writes nothing. A fact of another account than the file names, of the SYSTEM account, or in another state is counted and
left alone.

The action is written with `expected_id` = the listed fact: if its chain has moved on to a newer version, the adapter
refuses (MOVED) and the fact is skipped and listed, never followed to the newer document.

Reads GOOGLE_CLOUD_PROJECT and FIRESTORE_DATABASE from the environment or the repo's .env.

Usage:
    python -m scripts.migration.fact_p2_owner_decisions --decisions scripts/memory/decisions.json [--timezone Europe/Madrid] [--apply]
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from scripts.consolidation.plan_inputs import SelectionError, inside_memory  # noqa: E402
from scripts.migration import fact_p2_reopen_archives as reopen_script  # noqa: E402
from src.domain.entities import FactState  # noqa: E402
from src.domain.fact_expiry import EXPIRY_NONE  # noqa: E402
from src.domain.fact_review_reasons import owner_decided_reason  # noqa: E402
from src.domain.fact_update import ParsedFactUpdate  # noqa: E402
from src.domain.fact_update_outcome import UpdateRefusal  # noqa: E402
from src.domain.fact_words import written_text_problem  # noqa: E402
from src.domain.fact_write_context import FactWriteContext  # noqa: E402
from src.domain.pass_actor import PassActor  # noqa: E402

MEMORY_DIR = project_root / "scripts" / "memory"
ACTIONS = ("history", "current", "supersede", "invalidate", "revive")
_WRITES_OWN_FACT = frozenset({"history", "current", "revive"})

APPLIED = "applied"
CHANGED = "skipped_changed"
FAILED = "failed"


class DecisionsError(ValueError):
    """The decisions file is refused as a whole. The message names the entry by its number, never a text."""


@dataclass(frozen=True)
class Decision:
    fact_id: str
    account_id: str
    action: str
    text: Optional[str] = None
    successor_id: Optional[str] = None


def parse_decisions(raw: object) -> list[Decision]:
    """The decisions of a parsed file, checked in themselves (shape, action, texts, duplicates). Raises DecisionsError."""
    if not isinstance(raw, list) or not raw:
        raise DecisionsError("the decisions file is a non-empty JSON list")
    out: list[Decision] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise DecisionsError(f"decision {index}: not an object")
        fields = {}
        for name in ("fact_id", "account_id", "action"):
            value = item.get(name)
            if not isinstance(value, str) or not value.strip():
                raise DecisionsError(f"decision {index}: {name} is missing")
            fields[name] = value.strip()
        if fields["action"] not in ACTIONS:
            raise DecisionsError(f"decision {index}: unknown action")
        if fields["fact_id"] in seen:
            raise DecisionsError(f"decision {index}: the fact is listed twice")
        seen.add(fields["fact_id"])
        text, successor = item.get("text"), item.get("successor_id")
        if fields["action"] in ("history", "current"):
            problem = written_text_problem(text)
            if problem is not None:
                raise DecisionsError(f"decision {index}: text: {problem}")
        if fields["action"] == "supersede" and (
            not isinstance(successor, str) or not successor.strip() or successor.strip() == fields["fact_id"]
        ):
            raise DecisionsError(f"decision {index}: supersede needs a successor_id other than the fact")
        out.append(Decision(
            **fields, text=text.strip() if isinstance(text, str) else None,
            successor_id=successor.strip() if isinstance(successor, str) else None,
        ))
    return out


async def check_successors(db: Any, collection: str, decisions: list[Decision]) -> None:
    """Every supersede names a successor that is current in the store or is written earlier in this run. Raises
    DecisionsError (the file is refused)."""
    written: set[str] = set()
    for index, decision in enumerate(decisions, 1):
        if decision.action == "supersede" and decision.successor_id not in written:
            snap = await db.collection(collection).document(decision.successor_id).get(field_paths=["state", "account_id"])
            data = snap.to_dict() or {} if snap.exists else {}
            if reopen_script._plain(data.get("state")) != FactState.CURRENT.value or data.get("account_id") != decision.account_id:
                raise DecisionsError(f"decision {index}: the successor is not found as a current fact of the account")
        if decision.action in _WRITES_OWN_FACT:
            written.add(decision.fact_id)


def load_decisions(path: Path, memory_dir: Optional[Path] = None) -> list[Decision]:
    """The file under scripts/memory/, parsed and checked in itself."""
    try:
        raw = json.loads(inside_memory(path, memory_dir or MEMORY_DIR).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DecisionsError("the decisions file is not JSON") from exc
    return parse_decisions(raw)


def update_of(decision: Decision) -> ParsedFactUpdate:
    """The typed update of one decision."""
    if decision.action == "history":
        return ParsedFactUpdate(state=FactState.CURRENT, content=decision.text, expiry=EXPIRY_NONE, as_history=True)
    if decision.action == "current":
        return ParsedFactUpdate(state=FactState.CURRENT, content=decision.text, expiry=EXPIRY_NONE)
    if decision.action == "supersede":
        return ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by=decision.successor_id)
    if decision.action == "invalidate":
        return ParsedFactUpdate(state=FactState.INVALIDATED)
    return ParsedFactUpdate(state=FactState.CURRENT, expiry=EXPIRY_NONE)   # revive


def _last_reason(data: Mapping[str, Any]) -> Optional[str]:
    events = data.get("lifecycle_events") or []
    return events[-1].get("reason") if events else None


async def scan(
    db: Any, collection: str, decisions: list[Decision]
) -> tuple[list[tuple[Decision, Any, dict[str, Any], Any, bool]], dict[str, list[str]]]:
    """(decision, reference, data, update_time, resume) of each listed fact to write, and the ids left out by reason
    (`not_found`, `wrong_account`, `system_account`, `already_decided`, `not_closed`)."""
    left_out: dict[str, list[str]] = {
        "not_found": [], "wrong_account": [], "system_account": [], "already_decided": [], "not_closed": [],
    }
    found: list[tuple[Decision, Any, dict[str, Any], Any, bool]] = []
    for decision in decisions:
        snap = await db.collection(collection).document(decision.fact_id).get(field_paths=reopen_script.SCAN_FIELDS)
        if not snap.exists:
            left_out["not_found"].append(decision.fact_id)
            continue
        data = snap.to_dict() or {}
        state = reopen_script._plain(data.get("state"))
        reason = _last_reason(data)
        if data.get("account_id") != decision.account_id:
            left_out["wrong_account"].append(decision.fact_id)
        elif data.get("account_id") == reopen_script.SYSTEM_ACCOUNT_ID:
            left_out["system_account"].append(decision.fact_id)
        elif state == FactState.STALE.value and reason == reopen_script.EVENT_REASON:
            found.append((decision, snap.reference, data, snap.update_time, True))   # reopened by an earlier run
        elif reason == owner_decided_reason():
            left_out["already_decided"].append(decision.fact_id)
        elif state in reopen_script.REOPENABLE:
            found.append((decision, snap.reference, data, snap.update_time, False))
        else:
            left_out["not_closed"].append(decision.fact_id)
    return found, left_out


async def write_one(
    db: Any, port: Any, decision: Decision, ref: Any, data: dict[str, Any], update_time: Any, resume: bool,
    now: datetime, zone: str,
) -> tuple[str, Optional[str]]:
    """Reopen (unless resumed), then the typed action. (outcome, detail); the detail is a refusal's name or an error's
    type, never a text. Never raises."""
    try:
        if not resume:
            opened = await reopen_script.reopen(db, ref, data, update_time, now)
            if opened != reopen_script.REOPENED:
                return (CHANGED if opened == reopen_script.CHANGED else FAILED), "reopen"
        outcome = await port.apply_fact_update(
            decision.fact_id, update_of(decision), decision.account_id,
            context=FactWriteContext.for_pass(PassActor.OWNER, zone, None), evidence_at=now, reason=owner_decided_reason(),
            expected_id=decision.fact_id,   # the document he decided on: a chain that moved on is refused, not followed
        )
    except Exception as exc:  # the type only: a message may carry document content
        return FAILED, type(exc).__name__
    if outcome.refusal is UpdateRefusal.CONFLICT:
        return CHANGED, "conflict"
    if outcome.refusal is UpdateRefusal.MOVED:
        return CHANGED, "moved"   # the fact has a newer version than the one he decided on
    if outcome.refusal is not None:
        return FAILED, outcome.refusal.value
    return APPLIED, None


async def run(
    db: Any,
    collection: str,
    port: Any,
    decisions: list[Decision],
    apply: bool,
    out_dir: Path,
    now: Optional[datetime] = None,
    *,
    zone: str = "UTC",
) -> dict[str, int]:
    """Validate against the store, scan, report, and with `apply` write. Raises DecisionsError (a refused file) before
    anything is written; after that it never raises. Returns the counts it printed."""
    now = now or datetime.now(timezone.utc)
    await check_successors(db, collection, decisions)
    found, left_out = await scan(db, collection, decisions)
    counts = {
        "decisions": len(decisions), "to_write": len(found), "resumed": sum(1 for item in found if item[4]),
        **{name: len(ids) for name, ids in left_out.items()}, APPLIED: 0, CHANGED: 0, FAILED: 0,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    day = f"{now:%Y-%m-%d}"
    if apply:
        outcomes: dict[str, str] = {}
        details: dict[str, str] = {}
        for decision, ref, data, update_time, resume in found:
            outcome, detail = await write_one(db, port, decision, ref, data, update_time, resume, now, zone)
            outcomes[decision.fact_id] = outcome
            counts[outcome] += 1
            if detail is not None:
                details[decision.fact_id] = detail
        report: dict[str, Any] = {
            "generated_at": now.isoformat(), "collection": collection, "outcomes": outcomes, "details": details,
            "left_out": left_out,
        }
        path = out_dir / f"fact_p2_owner_decisions_{day}_applied.json"
    else:
        facts = [
            {
                "id": decision.fact_id, "action": decision.action, "account_prefix": decision.account_id[:8],
                "state": reopen_script._plain(data.get("state")), "old_text": data.get("text"),
                "new_text": decision.text if decision.action in ("history", "current") else data.get("text"),
                "successor_id": decision.successor_id, "resumes": resume,
            }
            for decision, _, data, _, resume in found
        ]
        report = {"generated_at": now.isoformat(), "collection": collection, "facts": facts, "left_out": left_out}
        path = out_dir / f"fact_p2_owner_decisions_{day}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    for name, value in counts.items():
        print(f"{name}: {value}")
    print(f"report: {path}")
    return counts


def build_port(db: Any) -> Any:
    """The typed fact-update port as production builds it (the fact-management adapter over the live repository)."""
    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.composition.service_container import ServiceContainer
    from src.config.settings import load_settings
    from src.services.search_enrichment_service import SearchEnrichmentService
    from src.domain.settings import SearchConfig

    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    container = ServiceContainer(config=config, db_client=db, env_config=env_config, account_repo=account_repo)
    search = SearchConfig()
    return container.create_fact_management_adapter(SearchEnrichmentService(
        repository=container.repository,
        embedding_service=container.embedding_service,
        keyword_limit=search.DEFAULT_KEYWORD_LIMIT,
        phrase_one_limit=search.DEFAULT_PHRASE_ONE_LIMIT,
        phrase_two_limit=search.DEFAULT_PHRASE_TWO_LIMIT,
        total_limit=search.DEFAULT_SEMANTIC_SEARCH_LIMIT,
    ))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decisions", type=Path, required=True, help="the owner's decisions, a JSON file under scripts/memory/")
    parser.add_argument("--timezone", default="UTC", help="the owner's zone for the write context (default UTC)")
    parser.add_argument("--apply", action="store_true", help="write the decisions (default: dry run)")
    args = parser.parse_args(argv)
    try:
        decisions = load_decisions(args.decisions, MEMORY_DIR)
    except (DecisionsError, SelectionError, OSError) as exc:
        parser.error(str(exc))
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    collection = EnvironmentConfig().domain_facts_collection

    async def go() -> None:   # the client is built inside the loop it runs on
        db = async_client()
        try:
            await run(db, collection, build_port(db), decisions, args.apply, MEMORY_DIR, zone=args.timezone)
        except DecisionsError as exc:
            parser.error(str(exc))

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
