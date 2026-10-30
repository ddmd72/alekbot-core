"""Search v2 port surface (USER_DRIVE_INDEXING_RFC §25.13.1): the two model ports and the repository additions.
Names, async-ness, parameters and their types are pinned: the plan parts are written against them. The repository
additions are abstract; both adapters implement them since P2."""
import inspect
import typing
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_enrichment import DriveHintDoc, InitialBuild, ModelUsageMonth
from src.domain.drive_enrichment_calls import HintReply, HintRequest, ProfileReply, ProfileRequest
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_index_lean import ChunkId, DriveChunkLean, DriveHintLean, DriveRecordLean
from src.domain.drive_index_params import INITIAL_BUILD_USD
from src.ports.document_profiler_port import DocumentProfilerPort
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.image_triage_port import ImageTriagePort
from src.ports.passage_hint_port import PassageHintPort

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def params(cls, name):
    signature = inspect.signature(getattr(cls, name))
    return [p for p in signature.parameters if p != "self"], {
        p.name: p.default for p in signature.parameters.values() if p.default is not inspect.Parameter.empty}


class TestModelPorts:
    @pytest.mark.parametrize("port, method, request_type, reply_type", [
        (DocumentProfilerPort, "profile", ProfileRequest, ProfileReply),
        (PassageHintPort, "hints", HintRequest, HintReply),
    ])
    def test_one_async_abstract_method(self, port, method, request_type, reply_type):
        assert set(port.__abstractmethods__) == {method}
        assert inspect.iscoroutinefunction(getattr(port, method))
        assert params(port, method) == (["request"], {})
        hints = typing.get_type_hints(getattr(port, method))
        assert hints["request"] is request_type and hints["return"] is reply_type
        with pytest.raises(TypeError):
            port()

    def test_image_triage_port_is_unchanged(self):
        """Its v2 reply (`TriageReply`) is defined; the port switches to it in P3."""
        assert set(ImageTriagePort.__abstractmethods__) == {"triage"}
        assert params(ImageTriagePort, "triage") == (["user_id", "account_id", "images"], {})


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

_RETURNS = {
    "find_by_about_keys": typing.List[DriveRecordLean],
    "find_expiring": typing.List[DriveRecordLean],
    "find_chunks_by_word_keys": typing.List[DriveChunkLean],
    "get_chunk_keys": typing.Dict[ChunkId, typing.Tuple[str, ...]],
    "find_hints_by_keys": typing.List[DriveHintLean],
    "get_hint_keys": typing.Dict[ChunkId, typing.Tuple[str, ...]],
    "find_nearest_hints": typing.List[typing.Tuple[DriveHintLean, float]],
    "get_hints": typing.List[DriveHintDoc],
    "get_hints_with_vectors": typing.List[typing.Tuple[DriveHintDoc, typing.List[float]]],
    "write_hints": type(None),
    "delete_hints_except": int,
    "delete_user_hints": int,
    "hints_page": typing.List[DriveHintLean],
    "enrich_queue": typing.List[DriveIndexRecord],
    "enrich_wakeups": typing.List[DriveIndexRecord],
    "count_by_job_state": typing.Dict[str, typing.Dict[str, int]],
    "keys_pending_page": typing.List[DriveIndexRecord],
    "get_records_lean": typing.Dict[str, DriveRecordLean],
    "get_model_usage": ModelUsageMonth,
    "add_model_usage": type(None),
    "get_initial_build": InitialBuild,
    "spend_initial_build": typing.Tuple[InitialBuild, float],
    "close_initial_build": type(None),
}


_SEQ = typing.Sequence[str]
_PARAM_TYPES = {   # parameter types the parts rely on (review MINOR 15)
    "find_by_about_keys": {"keys": _SEQ, "limit": int, "kind": typing.Optional[str],
                           "date_from": typing.Optional[datetime], "after": typing.Optional[str]},
    "find_expiring": {"after_date": date},
    "get_chunk_keys": {"ids": typing.Sequence[ChunkId]},
    "get_hint_keys": {"ids": typing.Sequence[ChunkId]},
    "find_nearest_hints": {"vector": typing.Sequence[float]},
    "write_hints": {"docs": typing.Sequence[DriveHintDoc], "vectors": typing.Sequence[typing.Sequence[float]]},
    "delete_hints_except": {"keep_generation": typing.Optional[int]},
    "hints_page": {"after": typing.Optional[ChunkId]},
    "enrich_wakeups": {"now": datetime},
    "count_by_job_state": {"kinds": typing.AbstractSet[str]},
    "enrich_queue": {"job": typing.Optional[str]},
    "add_model_usage": {"month": str, "day": str, "cls": str, "usd": float, "fallback_usd": float, "calls": int,
                        "covered_usd": float},
    "spend_initial_build": {"usd": float, "now": datetime},
}


class TestRepositoryV2Methods:
    @pytest.mark.parametrize("name", sorted(_V2_METHODS))
    def test_signature(self, name):
        method = getattr(DriveIndexRepository, name)
        assert inspect.iscoroutinefunction(method)
        assert params(DriveIndexRepository, name) == _V2_METHODS[name]
        hints = typing.get_type_hints(method)
        assert hints["return"] == _RETURNS[name]
        assert {p: hints[p] for p in _PARAM_TYPES.get(name, {})} == _PARAM_TYPES.get(name, {})

    def test_about_query_filters_are_keyword_only(self):
        signature = inspect.signature(DriveIndexRepository.find_by_about_keys)
        assert all(signature.parameters[p].kind is inspect.Parameter.KEYWORD_ONLY
                   for p in ("kind", "date_from", "date_to", "after"))

    def test_abstract(self):
        """An adapter that forgets one cannot be instantiated (review MINOR 13)."""
        assert set(_V2_METHODS) <= set(DriveIndexRepository.__abstractmethods__)

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
