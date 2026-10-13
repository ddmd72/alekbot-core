"""Search v2 port surface (USER_DRIVE_INDEXING_RFC §25.13.1): the two model ports and the repository additions.
Names, async-ness and parameters are pinned: the plan parts are written against them. The repository additions are
not abstract until P2 implements them in both adapters; until then they raise NotImplementedError."""
import inspect
import typing
from datetime import datetime, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import DriveHintDoc, InitialBuild, ModelUsageMonth
from src.domain.drive_enrichment_calls import HintReply, HintRequest, ProfileReply, ProfileRequest
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_index_lean import ChunkId, DriveChunkLean, DriveHintLean, DriveRecordLean
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


_V2_METHODS = {
    "find_by_about_keys": (["user_id", "keys", "limit", "kind", "date_from", "date_to", "after"],
                           {"kind": None, "date_from": None, "date_to": None, "after": None}),
    "find_expiring": (["user_id", "keys", "after_date", "limit"], {}),
    "find_chunks_by_word_keys": (["user_id", "keys", "limit", "after_item_id"], {"after_item_id": None}),
    "get_chunk_keys": (["user_id", "ids"], {}),
    "find_hints_by_keys": (["user_id", "keys", "limit"], {}),
    "find_nearest_hints": (["user_id", "vector", "limit", "kind"], {"kind": None}),
    "get_hints": (["user_id", "item_id", "generation"], {}),
    "write_hints": (["docs", "vectors"], {}),
    "delete_hints_except": (["user_id", "item_id", "keep_generation"], {}),
    "delete_user_hints": (["user_id", "limit"], {}),
    "hints_page": (["user_id", "after", "limit"], {}),
    "enrich_queue": (["user_id", "limit"], {}),
    "enrich_wakeups": (["user_id", "now", "limit"], {}),
    "count_by_job_state": (["user_id", "kinds"], {}),
    "keys_pending_page": (["user_id", "after", "limit"], {}),
    "get_records_lean": (["user_id", "item_ids"], {}),
    "get_model_usage": (["user_id", "month"], {}),
    "add_model_usage": (["user_id", "month", "day", "cls", "usd", "fallback_usd", "calls"], {}),
    "get_initial_build": (["user_id"], {}),
    "spend_initial_build": (["user_id", "usd", "now"], {}),
    "close_initial_build": (["user_id", "now"], {}),
}

_RETURNS = {
    "find_by_about_keys": typing.List[DriveRecordLean],
    "find_expiring": typing.List[DriveRecordLean],
    "find_chunks_by_word_keys": typing.List[DriveChunkLean],
    "get_chunk_keys": typing.Dict[ChunkId, typing.Tuple[str, ...]],
    "find_hints_by_keys": typing.List[DriveHintLean],
    "find_nearest_hints": typing.List[typing.Tuple[DriveHintLean, float]],
    "get_hints": typing.List[DriveHintDoc],
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
    "spend_initial_build": InitialBuild,
    "close_initial_build": type(None),
}


class TestRepositoryV2Methods:
    @pytest.mark.parametrize("name", sorted(_V2_METHODS))
    def test_signature(self, name):
        method = getattr(DriveIndexRepository, name)
        assert inspect.iscoroutinefunction(method)
        assert params(DriveIndexRepository, name) == _V2_METHODS[name]
        assert typing.get_type_hints(method)["return"] == _RETURNS[name]

    def test_about_query_filters_are_keyword_only(self):
        signature = inspect.signature(DriveIndexRepository.find_by_about_keys)
        assert all(signature.parameters[p].kind is inspect.Parameter.KEYWORD_ONLY
                   for p in ("kind", "date_from", "date_to", "after"))

    def test_not_abstract_until_p2(self):
        assert not set(_V2_METHODS) & set(DriveIndexRepository.__abstractmethods__)

    async def test_an_adapter_without_them_raises_not_implemented(self):
        repo = InMemoryDriveIndexRepository()
        with pytest.raises(NotImplementedError, match="Search v2 P2"):
            await repo.find_by_about_keys("u", ["e:rome"], 10)
        with pytest.raises(NotImplementedError, match="Search v2 P2"):
            await repo.get_initial_build("u")
