"""One-time migration of the owner-confirmed people list to stable entry ids (email index v2 slice 0).

    python scripts/people/assign_people_entry_ids.py [--show-names] [--apply]

`DEV_ACCOUNT_ID` comes from the environment file the other scripts use. Dry run by default: prints one line per person
(position, relation, id, status) and the checks, and writes nothing. `--apply` replaces the list through the store
(which assigns the ids), re-reads it and exits 1 when the stored ids differ from the plan. Names are printed only with
`--show-names`, to the owner's terminal; never redirect the output to a file."""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Callable, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.domain.confirmed_people import (  # noqa: E402
    ConfirmedPeople, ConfirmedPerson, EntryIdPlan, assign_entry_ids, confirmed_people_document, render_people_block,
)
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository  # noqa: E402


def _without_ids(people: ConfirmedPeople) -> ConfirmedPeople:
    return ConfirmedPeople(people.user, tuple(ConfirmedPerson(p.name, p.forms, p.relation) for p in people.people))


def _statuses(plan: EntryIdPlan) -> Dict[str, str]:
    status = {name: "kept" for name, _ in plan.kept}
    status.update({new: "renamed" for _, new, _ in plan.renamed})
    status.update({name: "new" for name, _ in plan.new})
    return status


async def run(repo: ConfirmedPeopleRepository, account_id: str, apply: bool, show_names: bool,
              out: Callable[[str], None] = print) -> int:
    stored = await repo.get(account_id)
    if stored is None:
        out("no confirmed people list: nothing to do")
        return 0
    assigned, plan = assign_entry_ids(stored, stored)
    status = _statuses(plan)
    for position, person in enumerate(assigned.people, 1):
        label = f"  {person.name}" if show_names else ""
        out(f"{position:>3}  {person.relation:<20} id={person.entry_id}  {status[person.name]}{label}")
    for name, entry_id in plan.retired:
        out(f"  -  retired            id={entry_id}" + (f"  {name}" if show_names else ""))
    out(f"people: {len(assigned.people)}  new: {len(plan.new)}  kept: {len(plan.kept)}  retired: {len(plan.retired)}"
        f"  next_entry_id: {assigned.next_entry_id}")
    if render_people_block(_without_ids(assigned).entries()) != render_people_block(assigned.entries()):
        out("CHECK FAILED: the people block changed")
        return 1
    out("check: the people block is byte-equal with and without ids (fact profiler: no re-profile)")
    if confirmed_people_document(assigned) == confirmed_people_document(stored):
        out("nothing to write")
        return 0
    if not apply:
        out("dry run: nothing written")
        return 0
    await repo.replace(account_id, stored, "owner_script")
    again = await repo.get(account_id)
    if again is None or [p.entry_id for p in again.people] != [p.entry_id for p in assigned.people] \
            or again.next_entry_id != assigned.next_entry_id:
        out("CHECK FAILED: the stored ids differ from the plan")
        return 1
    out(f"written: {len(again.people)} people, next_entry_id {again.next_entry_id}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--show-names", action="store_true", help="print names (owner's terminal only)")
    args = ap.parse_args()
    from scripts import _firestore   # loads the environment file at import
    from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
    from src.config.environment import EnvironmentConfig

    account_id = os.getenv("DEV_ACCOUNT_ID")
    if not account_id:
        sys.exit("DEV_ACCOUNT_ID is not set")
    repo = FirestoreConfirmedPeopleRepository(_firestore.async_client(), EnvironmentConfig())
    sys.exit(asyncio.run(run(repo, account_id, args.apply, args.show_names)))


if __name__ == "__main__":
    main()
