"""
Read-only smoke run of FirestoreDriveIndexRepository against live Firestore (USER_DRIVE_INDEXING_RFC §12.5; Search v2
reads §25.2.5, run before the v2 deploy as §25.8 rollout step 3 says).

Runs every read query of the repository once for one user id, so a missing or still-building index fails here with
Firestore's FAILED_PRECONDITION (and its index link) instead of on the first tick. Writes nothing. Empty results are
fine: index selection happens before any document is read. Run it after `make deploy-indexes` and
create_drive_vector_indexes.sh report READY, and before indexing is turned on for anyone.

The connection-scoped reads run under a made-up connection id, `smoke-conn` (MULTI_DRIVE_RFC §8): the queries are the
same whatever the id, and an index is per collection, so no real connection is needed.

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
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository  # noqa: E402
from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository  # noqa: E402
from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.drive_enrichment import EnrichJob, JobState  # noqa: E402
from src.domain.drive_index import Priority  # noqa: E402
from src.domain.drive_index_params import CONCEPT_KEYS_MAX  # noqa: E402
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS  # noqa: E402
from src.domain.drive_search import KIND_FAMILIES  # noqa: E402
from src.ports.drive_mirror_repository import ARCHIVE_FAMILY, NAME_FAMILY  # noqa: E402

_VECTOR = [1.0] + [0.0] * 767   # any non-zero 768-dim vector


SMOKE_CONNECTION = "smoke-conn"


def smoke_scope(user_id: str) -> ConnectionScope:
    """The scope the connection-scoped reads run under: the made-up connection `SMOKE_CONNECTION`."""
    return ConnectionScope(user_id, SMOKE_CONNECTION)


def checks(repo: FirestoreDriveIndexRepository, user_id: str,
           mirror_repo: Optional[FirestoreDriveMirrorRepository] = None) -> List[Tuple[str, Callable[[], Awaitable[object]]]]:
    """Every read the tick and the search issue, each once. The two within-rank eviction queries run directly:
    `lowest_ranked_carded` reaches them only when a carded record exists. The chunk page runs with a cursor, which
    uses the same index as the first page. The unread counts run one by one, over the content kinds the flip admits
    and the kinds it triages (text images), so a missing index is named per query. With `mirror_repo` the transcript
    mirror's reads follow (§27.9; `mirror_checks`): the record queue and wake-ups, the roots, every ledger query."""
    now = datetime.now(timezone.utc)
    scope = smoke_scope(user_id)
    dated, undated = repo.eviction_rank_queries(scope, 3)
    return [
        ("get_sync_state", lambda: repo.get_sync_state(scope)),
        ("list_sync_scopes", lambda: repo.list_sync_scopes()),
        ("get_records", lambda: repo.get_records(scope, ["smoke-item"])),
        ("due_records", lambda: repo.due_records(scope, now, 10)),
        *[(f"oldest_due_at[{p.name}]", (lambda p=p: repo.oldest_due_at(scope, int(p)))) for p in Priority],
        ("children", lambda: repo.children(scope, "smoke-parent", "a", 5)),
        ("records_page", lambda: repo.records_page(scope, "a", 5)),
        ("records_seen_before", lambda: repo.records_seen_before(scope, 1, 5)),
        ("count_carded", lambda: repo.count_carded(scope)),
        ("count_by_state", lambda: repo.count_by_state(scope)),
        ("lowest_ranked_carded", lambda: repo.lowest_ranked_carded(scope)),
        ("eviction: oldest dated within a rank", lambda: dated.get()),
        ("eviction: undated within a rank", lambda: undated.get()),
        ("card_capped", lambda: repo.card_capped(scope, 5)),
        ("chunk_capped", lambda: repo.chunk_capped(scope, 5)),
        *[(f"count_unread[{n}]", (lambda q=q: q.count(alias="n").get()))
          for n, q in enumerate(repo.unread_count_queries(scope, SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS))],
        ("find_nearest_cards", lambda: repo.find_nearest_cards(scope, _VECTOR, 5)),
        ("find_nearest_cards[kind]", lambda: repo.find_nearest_cards(scope, _VECTOR, 5, kind="pdf")),
        ("find_by_name_keys", lambda: repo.find_by_name_keys(scope, ["w:smoke"], 5)),
        ("find_by_name_keys[kind]", lambda: repo.find_by_name_keys(scope, ["w:smoke"], 5, kind="pdf")),
        ("filter_records", lambda: repo.filter_records(scope, None, None, None, 5)),
        ("filter_records[kind+dates]",
         lambda: repo.filter_records(scope, "pdf", now - timedelta(days=7), now, 5)),
        ("card_vectors", lambda: repo.card_vectors(scope, ["smoke-item"])),
        # chunks (§24.6); the two deletes are not run: their reads are equality-only (single-field indexes)
        ("readable_chunk_total", lambda: repo.readable_chunk_total(scope)),
        ("find_nearest_chunks", lambda: repo.find_nearest_chunks(scope, _VECTOR, 5)),
        ("find_nearest_chunks[kind]", lambda: repo.find_nearest_chunks(scope, _VECTOR, 5, kind="pdf")),
        ("find_chunks_by_term", lambda: repo.find_chunks_by_term(scope, "20240012", 5)),
        ("find_chunks_by_term[kind]", lambda: repo.find_chunks_by_term(scope, "20240012", 5, kind="pdf")),
        ("chunks_page", lambda: repo.chunks_page(scope, ("smoke-item", 1, 0), 5)),
        ("get_chunks", lambda: repo.get_chunks(scope, "smoke-item", 1, [0, 1])),
        ("get_chunks_with_vectors", lambda: repo.get_chunks_with_vectors(scope, "smoke-item", 1, [0, 1])),
        *v2_checks(repo, user_id, now),
        # files the bot cannot open (§26.2, §26.9 step 3): the tick's counters and the fan-out's notice
        ("inaccessible_records", lambda: repo.inaccessible_records(scope, 5)),
        *transcript_checks(repo, user_id, now),
        *(mirror_checks(repo, mirror_repo, user_id, now) if mirror_repo is not None else []),
    ]


def v2_checks(repo: FirestoreDriveIndexRepository, user_id: str,
              now: datetime) -> List[Tuple[str, Callable[[], Awaitable[object]]]]:
    """The Search v2 reads (§25.2.5). The about stream runs once with the most disjunctions a query may carry
    (CONCEPT_KEYS_MAX keys and a single kind); a cursor read of a record that does not exist sends no query, so the
    about cursor is not run (its query is the same as the first page's). A kind family runs its kNN one member kind
    at a time, as the search does (§25.5.5): one check over the largest family. The per-state counts run one by one,
    so a missing index is named per query. The month's model usage and the initial build are the user's and the
    provider account's, not a connection's."""
    scope = smoke_scope(user_id)
    keys = [f"w:smoke{i}" for i in range(CONCEPT_KEYS_MAX)]
    members = sorted(max(KIND_FAMILIES.values(), key=len))
    ids = [("smoke-item", 1, 0), ("smoke-item", 1, 1)]
    per_batch = len(EnrichJob) * len(JobState)   # count queries per kind batch (job_state_count_queries)
    return [
        ("find_by_about_keys", lambda: repo.find_by_about_keys(scope, ["e:smoke"], 5)),
        ("find_by_about_keys[kind]", lambda: repo.find_by_about_keys(scope, ["e:smoke"], 5, kind="pdf")),
        ("find_by_about_keys[dates]",
         lambda: repo.find_by_about_keys(scope, ["e:smoke"], 5, date_from=now - timedelta(days=7), date_to=now)),
        ("find_by_about_keys[30 keys+kind]", lambda: repo.find_by_about_keys(scope, keys, 5, kind="pdf")),
        ("find_expiring", lambda: repo.find_expiring(scope, ["lv:smoke"], now.date(), 5)),
        ("find_chunks_by_word_keys", lambda: repo.find_chunks_by_word_keys(scope, keys, 5)),
        ("find_chunks_by_word_keys[cursor]",
         lambda: repo.find_chunks_by_word_keys(scope, ["w:smoke"], 5, after_item_id="smoke-item")),
        ("get_chunk_keys", lambda: repo.get_chunk_keys(scope, ids)),
        ("find_hints_by_keys", lambda: repo.find_hints_by_keys(scope, keys, 5)),
        ("find_hints_by_keys[cursor]",
         lambda: repo.find_hints_by_keys(scope, ["w:smoke"], 5, after_item_id="smoke-item")),
        ("get_hint_keys", lambda: repo.get_hint_keys(scope, ids)),
        ("find_nearest_hints", lambda: repo.find_nearest_hints(scope, _VECTOR, 5)),
        ("find_nearest_hints[kind]", lambda: repo.find_nearest_hints(scope, _VECTOR, 5, kind="pdf")),
        (f"family kNN[{','.join(members)}]", lambda: _family_knn(repo, scope, members)),
        ("get_hints", lambda: repo.get_hints(scope, "smoke-item", 1)),
        ("get_hints_with_vectors", lambda: repo.get_hints_with_vectors(scope, "smoke-item", 1)),
        ("hints_page", lambda: repo.hints_page(scope, ("smoke-item", 1, 0), 5)),
        ("enrich_queue", lambda: repo.enrich_queue(scope, 5)),
        ("enrich_queue[profile]", lambda: repo.enrich_queue(scope, 5, job="profile")),
        ("enrich_queue[passages]", lambda: repo.enrich_queue(scope, 5, job="passages")),
        ("enrich_wakeups", lambda: repo.enrich_wakeups(scope, now, 5)),
        *[(f"count_by_job_state[{job}={state}, batch {n // per_batch}]", (lambda q=q: q.count(alias="n").get()))
          for n, (job, state, q) in enumerate(repo.job_state_count_queries(scope, SLICE2_CONTENT_KINDS))],
        ("keys_pending_page", lambda: repo.keys_pending_page(scope, "a", 5)),
        ("get_records_lean", lambda: repo.get_records_lean(scope, ["smoke-item"])),
        ("get_model_usage", lambda: repo.get_model_usage(user_id, now.strftime("%Y-%m"))),
        ("get_initial_build", lambda: repo.get_initial_build(user_id, "smoke-realm", "smoke-subject")),
    ]


def transcript_checks(repo: FirestoreDriveIndexRepository, user_id: str,
                      now: datetime) -> List[Tuple[str, Callable[[], Awaitable[object]]]]:
    """The transcript reads (§27.3-§27.5, §27.8 b), all read-only: the record queues (queue, wake-ups, in-flight by state), the copy check, the open
    batches, the orphan listing, the key-in-use check, and the two collection-group reads the orphan sweep sends (`windows`, `parts`;
    the sweep itself deletes, so only its query runs here). The jobs, batches and records are a connection's; the
    paid transcripts (the orphan listing, the key-in-use check) are the user's (I8)."""
    scope = smoke_scope(user_id)
    return [
        ("transcript_queue", lambda: repo.transcript_queue(scope, 5)),
        ("transcript_wakeups", lambda: repo.transcript_wakeups(scope, now, 5)),
        ("transcript_records", lambda: repo.transcript_records(scope, "working", 5)),
        ("jobs_by_content_key", lambda: repo.jobs_by_content_key(scope, "smoke-key")),
        ("get_open_batches", lambda: repo.get_open_batches(scope, now, 5)),
        ("orphan_transcripts", lambda: repo.orphan_transcripts(user_id, now, 5)),
        ("transcript_key_in_use", lambda: repo.transcript_key_in_use(user_id, "smoke-key")),
        ("orphaned windows (collection group)",
         lambda: repo.connection_subdoc_refs(repo.windows_group, scope.connection_id, 5)),
        ("orphaned parts (collection group)",
         lambda: repo.user_subdoc_refs(repo.parts_group, user_id, 5)),
    ]


def mirror_checks(repo: FirestoreDriveIndexRepository, mirror: FirestoreDriveMirrorRepository, user_id: str,
                  now: datetime) -> List[Tuple[str, Callable[[], Awaitable[object]]]]:
    """The transcript mirror's reads (§27.9, §27.15 step 8c), all read-only: the records' mirror queue, wake-ups and the
    seed pass's cursor read, then the roots and every ledger query, each once, so a missing composite index is named per
    query. The purge's scanning delete (`delete_user_transcripts_except`) deletes, so only its page query's shape
    (equality and the document id, single-field indexes) is not run here."""
    scope = smoke_scope(user_id)
    drive, key, keys = "smoke-drive", "smoke-user:file:smoke-item", ["w:smoke"]
    return [
        ("mirror_queue", lambda: repo.mirror_queue(scope, 5)),
        ("mirror_wakeups", lambda: repo.mirror_wakeups(scope, now, 5)),
        ("transcript_records[after]", lambda: repo.transcript_records(scope, "current", 5, after="smoke-item")),
        ("mirror roots", lambda: mirror.roots(user_id)),
        ("mirror get_root", lambda: mirror.get_root(user_id, drive)),
        ("mirror get_docs", lambda: mirror.get_docs(user_id, [key])),
        ("mirror by_mirror_item", lambda: mirror.by_mirror_item(user_id, "smoke-mirror-item")),
        ("mirror ready_marks", lambda: mirror.ready_marks(user_id, drive, now, 5)),
        ("mirror children", lambda: mirror.children(user_id, drive, "smoke-parent", 5)),
        ("mirror folder_docs", lambda: mirror.folder_docs(user_id, drive, None, 5)),
        ("mirror folder_docs[cursor]", lambda: mirror.folder_docs(user_id, drive, key, 5)),
        ("mirror scan_page", lambda: mirror.scan_page(user_id, drive, None, 5)),
        ("mirror scan_page[cursor]", lambda: mirror.scan_page(user_id, drive, key, 5)),
        ("mirror detached_due", lambda: mirror.detached_due(user_id, drive, now, 5)),
        ("mirror archived_newest", lambda: mirror.archived_newest(user_id, 5)),
        ("mirror archived_by_keys[archive]", lambda: mirror.archived_by_keys(user_id, ARCHIVE_FAMILY, keys, 5)),
        ("mirror archived_by_keys[name]", lambda: mirror.archived_by_keys(user_id, NAME_FAMILY, keys, 5)),
        ("mirror pinned", lambda: mirror.pinned(user_id, "smoke-key")),
        ("mirror pinned_keys", lambda: mirror.pinned_keys(user_id)),
    ]


async def _family_knn(repo: FirestoreDriveIndexRepository, scope: ConnectionScope, kinds: Sequence[str]) -> None:
    """A family's kNN as the search runs it: card, chunk and hint kNN once per member kind (RFC Δ37, §25.5.2)."""
    for kind in kinds:
        await repo.find_nearest_cards(scope, _VECTOR, 5, kind=kind)
        await repo.find_nearest_chunks(scope, _VECTOR, 5, kind=kind)
        await repo.find_nearest_hints(scope, _VECTOR, 5, kind=kind)


async def run_smoke(repo: FirestoreDriveIndexRepository, user_id: str,
                    mirror_repo: Optional[FirestoreDriveMirrorRepository] = None) -> List[Tuple[str, str]]:
    """Run every check; return (name, error) for each one that raised."""
    failures: List[Tuple[str, str]] = []
    for name, call in checks(repo, user_id, mirror_repo):
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
    mirror = FirestoreDriveMirrorRepository(db, env)
    failures = await run_smoke(repo, user_id, mirror)
    print(f"{len(failures)} of {len(checks(repo, user_id, mirror))} queries failed" if failures else "All queries ran.")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("user_id", help="user whose index the queries read (any id; an empty index is fine)")
    sys.exit(asyncio.run(_main(parser.parse_args().user_id)))
