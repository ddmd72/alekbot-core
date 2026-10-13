"""
Read-only smoke run of FirestoreDriveIndexRepository against live Firestore (USER_DRIVE_INDEXING_RFC §12.5; Search v2
reads §25.2.5, run before the v2 deploy as §25.8 rollout step 3 says).

Runs every read query of the repository once for one user id, so a missing or still-building index fails here with
Firestore's FAILED_PRECONDITION (and its index link) instead of on the first tick. Writes nothing. Empty results are
fine: index selection happens before any document is read. Run it after `make deploy-indexes` and
create_drive_vector_indexes.sh report READY, and before indexing is turned on for anyone.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_index_smoke.py <user_id>
Exit code: 0 when every query ran, 1 otherwise.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository  # noqa: E402
from src.domain.drive_index import Priority  # noqa: E402
from src.domain.drive_index_params import CONCEPT_KEYS_MAX  # noqa: E402
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS  # noqa: E402
from src.domain.drive_search import KIND_FAMILIES  # noqa: E402

_VECTOR = [1.0] + [0.0] * 767   # any non-zero 768-dim vector


def checks(repo: FirestoreDriveIndexRepository, user_id: str) -> List[Tuple[str, Callable[[], Awaitable[object]]]]:
    """Every read the tick and the search issue, each once. The two within-rank eviction queries run directly:
    `lowest_ranked_carded` reaches them only when a carded record exists. The chunk page runs with a cursor, which
    uses the same index as the first page. The unread counts run one by one, over the content kinds the flip admits
    and the kinds it triages (text images), so a missing index is named per query."""
    now = datetime.now(timezone.utc)
    dated, undated = repo.eviction_rank_queries(user_id, 3)
    return [
        ("get_sync_state", lambda: repo.get_sync_state(user_id)),
        ("list_sync_user_ids", lambda: repo.list_sync_user_ids()),
        ("get_records", lambda: repo.get_records(user_id, ["smoke-item"])),
        ("due_records", lambda: repo.due_records(user_id, now, 10)),
        *[(f"oldest_due_at[{p.name}]", (lambda p=p: repo.oldest_due_at(user_id, int(p)))) for p in Priority],
        ("children", lambda: repo.children(user_id, "smoke-parent", "a", 5)),
        ("records_page", lambda: repo.records_page(user_id, "a", 5)),
        ("records_seen_before", lambda: repo.records_seen_before(user_id, 1, 5)),
        ("count_carded", lambda: repo.count_carded(user_id)),
        ("count_by_state", lambda: repo.count_by_state(user_id)),
        ("lowest_ranked_carded", lambda: repo.lowest_ranked_carded(user_id)),
        ("eviction: oldest dated within a rank", lambda: dated.get()),
        ("eviction: undated within a rank", lambda: undated.get()),
        ("card_capped", lambda: repo.card_capped(user_id, 5)),
        ("chunk_capped", lambda: repo.chunk_capped(user_id, 5)),
        *[(f"count_unread[{n}]", (lambda q=q: q.count(alias="n").get()))
          for n, q in enumerate(repo.unread_count_queries(user_id, SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS))],
        ("find_nearest_cards", lambda: repo.find_nearest_cards(user_id, _VECTOR, 5)),
        ("find_nearest_cards[kind]", lambda: repo.find_nearest_cards(user_id, _VECTOR, 5, kind="pdf")),
        ("find_by_name_keys", lambda: repo.find_by_name_keys(user_id, ["w:smoke"], 5)),
        ("find_by_name_keys[kind]", lambda: repo.find_by_name_keys(user_id, ["w:smoke"], 5, kind="pdf")),
        ("filter_records", lambda: repo.filter_records(user_id, None, None, None, 5)),
        ("filter_records[kind+dates]",
         lambda: repo.filter_records(user_id, "pdf", now - timedelta(days=7), now, 5)),
        ("card_vectors", lambda: repo.card_vectors(user_id, ["smoke-item"])),
        # chunks (§24.6); the two deletes are not run: their reads are equality-only (single-field indexes)
        ("readable_chunk_total", lambda: repo.readable_chunk_total(user_id)),
        ("find_nearest_chunks", lambda: repo.find_nearest_chunks(user_id, _VECTOR, 5)),
        ("find_nearest_chunks[kind]", lambda: repo.find_nearest_chunks(user_id, _VECTOR, 5, kind="pdf")),
        ("find_chunks_by_term", lambda: repo.find_chunks_by_term(user_id, "20240012", 5)),
        ("find_chunks_by_term[kind]", lambda: repo.find_chunks_by_term(user_id, "20240012", 5, kind="pdf")),
        ("chunks_page", lambda: repo.chunks_page(user_id, ("smoke-item", 1, 0), 5)),
        ("get_chunks", lambda: repo.get_chunks(user_id, "smoke-item", 1, [0, 1])),
        ("get_chunks_with_vectors", lambda: repo.get_chunks_with_vectors(user_id, "smoke-item", 1, [0, 1])),
        *v2_checks(repo, user_id, now),
    ]


def v2_checks(repo: FirestoreDriveIndexRepository, user_id: str,
              now: datetime) -> List[Tuple[str, Callable[[], Awaitable[object]]]]:
    """The Search v2 reads (§25.2.5). The about stream runs once with the most disjunctions a query may carry
    (CONCEPT_KEYS_MAX keys and a single kind); a cursor read of a record that does not exist sends no query, so the
    about cursor is not run (its query is the same as the first page's). A kind family runs its kNN one member kind
    at a time, as the search does (§25.5.5): one check over the largest family. The per-state counts run one by one,
    so a missing index is named per query."""
    keys = [f"w:smoke{i}" for i in range(CONCEPT_KEYS_MAX)]
    members = sorted(max(KIND_FAMILIES.values(), key=len))
    ids = [("smoke-item", 1, 0), ("smoke-item", 1, 1)]
    return [
        ("find_by_about_keys", lambda: repo.find_by_about_keys(user_id, ["e:smoke"], 5)),
        ("find_by_about_keys[kind]", lambda: repo.find_by_about_keys(user_id, ["e:smoke"], 5, kind="pdf")),
        ("find_by_about_keys[dates]",
         lambda: repo.find_by_about_keys(user_id, ["e:smoke"], 5, date_from=now - timedelta(days=7), date_to=now)),
        ("find_by_about_keys[30 keys+kind]", lambda: repo.find_by_about_keys(user_id, keys, 5, kind="pdf")),
        ("find_expiring", lambda: repo.find_expiring(user_id, ["lv:smoke"], now.date(), 5)),
        ("find_chunks_by_word_keys", lambda: repo.find_chunks_by_word_keys(user_id, keys, 5)),
        ("find_chunks_by_word_keys[cursor]",
         lambda: repo.find_chunks_by_word_keys(user_id, ["w:smoke"], 5, after_item_id="smoke-item")),
        ("get_chunk_keys", lambda: repo.get_chunk_keys(user_id, ids)),
        ("find_hints_by_keys", lambda: repo.find_hints_by_keys(user_id, keys, 5)),
        ("find_hints_by_keys[cursor]",
         lambda: repo.find_hints_by_keys(user_id, ["w:smoke"], 5, after_item_id="smoke-item")),
        ("get_hint_keys", lambda: repo.get_hint_keys(user_id, ids)),
        ("find_nearest_hints", lambda: repo.find_nearest_hints(user_id, _VECTOR, 5)),
        ("find_nearest_hints[kind]", lambda: repo.find_nearest_hints(user_id, _VECTOR, 5, kind="pdf")),
        (f"family kNN[{','.join(members)}]", lambda: _family_knn(repo, user_id, members)),
        ("get_hints", lambda: repo.get_hints(user_id, "smoke-item", 1)),
        ("get_hints_with_vectors", lambda: repo.get_hints_with_vectors(user_id, "smoke-item", 1)),
        ("hints_page", lambda: repo.hints_page(user_id, ("smoke-item", 1, 0), 5)),
        ("enrich_queue", lambda: repo.enrich_queue(user_id, 5)),
        ("enrich_wakeups", lambda: repo.enrich_wakeups(user_id, now, 5)),
        *[(f"count_by_job_state[{job}={state}, batch {n // 12}]", (lambda q=q: q.count(alias="n").get()))
          for n, (job, state, q) in enumerate(repo.job_state_count_queries(user_id, SLICE2_CONTENT_KINDS))],
        ("keys_pending_page", lambda: repo.keys_pending_page(user_id, "a", 5)),
        ("get_records_lean", lambda: repo.get_records_lean(user_id, ["smoke-item"])),
        ("get_model_usage", lambda: repo.get_model_usage(user_id, now.strftime("%Y-%m"))),
        ("get_initial_build", lambda: repo.get_initial_build(user_id)),
    ]


async def _family_knn(repo: FirestoreDriveIndexRepository, user_id: str, kinds: Sequence[str]) -> None:
    """A family's kNN as the search runs it: card, chunk and hint kNN once per member kind (RFC Δ37, §25.5.2)."""
    for kind in kinds:
        await repo.find_nearest_cards(user_id, _VECTOR, 5, kind=kind)
        await repo.find_nearest_chunks(user_id, _VECTOR, 5, kind=kind)
        await repo.find_nearest_hints(user_id, _VECTOR, 5, kind=kind)


async def run_smoke(repo: FirestoreDriveIndexRepository, user_id: str) -> List[Tuple[str, str]]:
    """Run every check; return (name, error) for each one that raised."""
    failures: List[Tuple[str, str]] = []
    for name, call in checks(repo, user_id):
        try:
            await call()
            print(f"✅ {name}")
        except Exception as exc:   # report every failure, then fail the run
            print(f"❌ {name}: {type(exc).__name__}: {exc}")
            failures.append((name, f"{type(exc).__name__}: {exc}"))
    return failures


async def _main(user_id: str) -> int:
    from google.cloud import firestore

    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    failures = await run_smoke(repo, user_id)
    print(f"{len(failures)} of {len(checks(repo, user_id))} queries failed" if failures else "All queries ran.")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("user_id", help="user whose index the queries read (any id; an empty index is fine)")
    sys.exit(asyncio.run(_main(parser.parse_args().user_id)))
