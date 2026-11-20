"""Search v2 repository additions (USER_DRIVE_INDEXING_RFC §25.13.1): both adapters implement them since P2."""
import inspect
from unittest.mock import MagicMock
from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_enrichment import InitialBuild
from src.domain.drive_index_params import INITIAL_BUILD_USD


_V2_METHODS = {   # connection-scoped reads and writes take a ConnectionScope (MULTI_DRIVE_RFC §8)
    "find_by_about_keys": (["scope", "keys", "limit", "kind", "date_from", "date_to", "after"],
                           {"kind": None, "date_from": None, "date_to": None, "after": None}),
    "find_expiring": (["scope", "keys", "after_date", "limit"], {}),
    "find_chunks_by_word_keys": (["scope", "keys", "limit", "after_item_id"], {"after_item_id": None}),
    "get_chunk_keys": (["scope", "ids"], {}),
    "find_hints_by_keys": (["scope", "keys", "limit", "after_item_id"], {"after_item_id": None}),
    "get_hint_keys": (["scope", "ids"], {}),
    "find_nearest_hints": (["scope", "vector", "limit", "kind"], {"kind": None}),
    "get_hints": (["scope", "item_id", "generation"], {}),
    "get_hints_with_vectors": (["scope", "item_id", "generation"], {}),
    "write_hints": (["docs", "vectors"], {}),
    "delete_hints_except": (["scope", "item_id", "keep_generation"], {}),
    "delete_user_hints": (["scope", "limit"], {}),
    "hints_page": (["scope", "after", "limit"], {}),
    # P5 review MINOR 6: one agent type's own queue while the other is backed off (`job`).
    "enrich_queue": (["scope", "limit", "job"], {"job": None}),
    "enrich_wakeups": (["scope", "now", "limit"], {}),
    "count_by_job_state": (["scope", "kinds"], {}),
    "keys_pending_page": (["scope", "after", "limit"], {}),
    "get_records_lean": (["scope", "item_ids"], {}),
    "get_model_usage": (["user_id", "month"], {}),
    # P5 review M1: the initial build's covered share is recorded apart from the month's cap counter.
    "add_model_usage": (["user_id", "month", "day", "cls", "usd", "fallback_usd", "calls", "covered_usd"],
                        {"covered_usd": 0.0}),
    # MULTI_DRIVE_RFC §10 (D8): the initial build is keyed by the provider account, not the user alone
    "get_initial_build": (["user_id", "realm", "subject"], {}),
    "spend_initial_build": (["user_id", "realm", "subject", "usd", "now"], {}),
    "close_initial_build": (["user_id", "realm", "subject", "now"], {}),
}


class TestRepositoryV2Methods:
    async def test_both_adapters_implement_them_since_p2(self, mock_env_config):
        """The Part 0 scaffold flipped by P2 (§25.13.8): each adapter defines every v2 method itself, none of them a
        stub, and the in-memory one answers an empty index."""
        for cls in (InMemoryDriveIndexRepository, FirestoreDriveIndexRepository):
            for name in _V2_METHODS:
                assert name in vars(cls), (cls.__name__, name)
                source = inspect.getsource(inspect.unwrap(vars(cls)[name]))
                assert "NotImplementedError" not in source and "SEARCH_V2_PENDING" not in source, (cls.__name__, name)
        FirestoreDriveIndexRepository(MagicMock(), mock_env_config)   # constructible: no abstract method left
        repo = InMemoryDriveIndexRepository()
        assert await repo.find_by_about_keys(ConnectionScope("u", "c"), ["e:rome"], 10) == []
        assert await repo.get_initial_build("u", "realm-1", "subj-1") == InitialBuild(allowance_usd=INITIAL_BUILD_USD)
