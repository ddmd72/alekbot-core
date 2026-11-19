"""A stored companion binding's `session_domains` read through the domain registry (fact profile RFC §6 row 8): an old
`FactDomain` value becomes the registry keys that replace it, a registry key stays itself, a value that maps to nothing
is dropped with a warning, and loading never raises on a domain value. On the evaluating Firestore fake, through
`FirestoreChannelBindingAdapter.get` and `save`; assertions on what `get` returns and on the raw stored document."""
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from src.adapters.firestore_channel_binding_adapter import FirestoreChannelBindingAdapter
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.fact_domains import Domain, config_key
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_channel_bindings"


@pytest.fixture
def db() -> EvalDb:
    return EvalDb(indexes=INDEXES_FILE)


@pytest.fixture
def adapter(db) -> FirestoreChannelBindingAdapter:
    return FirestoreChannelBindingAdapter(db, SimpleNamespace(firestore_collection_prefix="eval_"))


def _store(db, session_domains: List[Any]) -> None:
    db.collection(COL).docs["C1"] = {
        "channel_id": "C1", "agent_type": "tutor", "intent": "tutor_chat", "created_by": "u1",
        "companion_config": {"window_threshold": 40, "batch_size": 20, "include_biographical": True,
                             "session_domains": session_domains},
    }


async def _loaded(adapter, db, session_domains: List[Any]) -> List[str]:
    _store(db, session_domains)
    binding = await adapter.get("C1")
    assert binding is not None and binding.companion_config is not None
    return binding.companion_config.session_domains


async def test_an_old_value_reads_as_the_registry_keys_that_replace_it(adapter, db):
    assert await _loaded(adapter, db, ["education"]) == [Domain.LEARNING]
    assert await _loaded(adapter, db, ["finance"]) == [Domain.MONEY, Domain.SAVINGS]


async def test_an_old_value_with_many_successors_reads_as_all_of_them_in_registry_order(adapter, db):
    assert await _loaded(adapter, db, ["preference"]) == list(config_key("preference"))
    assert Domain.SPORT in config_key("preference")


async def test_a_registry_key_is_kept(adapter, db):
    assert await _loaded(adapter, db, [Domain.SPORT, Domain.TRANSPORT]) == [Domain.SPORT, Domain.TRANSPORT]


async def test_old_values_and_keys_together_keep_first_seen_order_without_repeats(adapter, db):
    assert await _loaded(adapter, db, ["skill", Domain.ABILITIES, Domain.SPORT, "education"]) == [
        Domain.ABILITIES, Domain.SPORT, Domain.LEARNING]


async def test_an_unknown_value_is_dropped_with_a_warning(adapter, db, caplog):
    with caplog.at_level(logging.WARNING):
        got = await _loaded(adapter, db, ["education", "astrology"])

    assert got == [Domain.LEARNING]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("astrology" in w and "session_domains" in w for w in warnings)


async def test_only_unknown_values_never_raise_and_read_as_no_domains(adapter, db, caplog):
    with caplog.at_level(logging.WARNING):
        got = await _loaded(adapter, db, ["astrology", "", "BIOGRAPHICAL"])

    assert got == []
    assert any("astrology" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_known_values_load_without_a_warning(adapter, db, caplog):
    with caplog.at_level(logging.WARNING):
        await _loaded(adapter, db, ["education", Domain.SPORT])

    assert [r for r in caplog.records if "session_domains" in r.getMessage()] == []


async def test_a_mapped_binding_saves_its_registry_keys_and_reads_back_the_same(adapter, db):
    _store(db, ["education", "astrology"])
    loaded = await adapter.get("C1")

    await adapter.save(loaded)

    assert db.collection(COL).docs["C1"]["companion_config"]["session_domains"] == [Domain.LEARNING]
    assert await adapter.get("C1") == loaded


async def test_a_new_binding_stores_its_registry_keys_as_given(adapter, db):
    config = CompanionConfig(window_threshold=40, batch_size=20, session_domains=[Domain.SPORT, Domain.LEARNING])
    await adapter.save(ChannelBinding(channel_id="C2", agent_type="tutor", intent="tutor_chat", created_by="u1",
                                      companion_config=config))

    assert db.collection(COL).docs["C2"]["companion_config"]["session_domains"] == [Domain.SPORT, Domain.LEARNING]
    assert (await adapter.get("C2")).companion_config.session_domains == [Domain.SPORT, Domain.LEARNING]
