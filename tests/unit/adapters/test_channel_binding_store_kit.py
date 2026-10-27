"""ChannelBindingPort kit (P11, G3 store 3): the docstring of `src/ports/channel_binding_port.py` checked against
`FirestoreChannelBindingAdapter` on the evaluating Firestore fake (H1), with the index refusal ON (the adapter issues no
query). Assertions are on the raw stored documents and on what `get` returns.

`_adapter(db)` is the one place the adapter is constructed.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_channel_binding_adapter import FirestoreChannelBindingAdapter
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig, CompanionTextMode
from src.domain.entities import FactDomain
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_channel_bindings"


def _adapter(db: EvalDb) -> FirestoreChannelBindingAdapter:
    return FirestoreChannelBindingAdapter(db, SimpleNamespace(firestore_collection_prefix="eval_"))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE)
        self.adapter = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(COL).docs


@pytest.fixture
def store():
    return Store()


FULL = CompanionConfig(window_threshold=30, batch_size=20, text_mode=CompanionTextMode.FULL, include_biographical=True,
                       session_domains=[FactDomain.BIOGRAPHICAL, FactDomain.PREFERENCE],
                       include_standing_directives=True, include_own_records=False, history_recent_full_turns=3)


def binding(channel_id="C1", created_by="u1", companion_config=None) -> ChannelBinding:
    return ChannelBinding(channel_id=channel_id, agent_type="tutor", intent="tutor_chat", created_by=created_by,
                          companion_config=companion_config)


def raw_binding(store, companion_config, channel_id="C1") -> None:
    store.docs[channel_id] = {"channel_id": channel_id, "agent_type": "tutor", "intent": "tutor_chat",
                              "created_by": "u1", "companion_config": companion_config}


async def test_save_then_get_round_trips_every_field_and_stores_enums_as_values(store):
    await store.adapter.save(binding(companion_config=FULL))
    assert await store.adapter.get("C1") == binding(companion_config=FULL)
    assert store.docs["C1"] == {
        "channel_id": "C1", "agent_type": "tutor", "intent": "tutor_chat", "created_by": "u1",
        "companion_config": {"window_threshold": 30, "batch_size": 20, "text_mode": "full", "include_biographical": True,
                             "session_domains": ["biographical", "preference"], "include_standing_directives": True,
                             "include_own_records": False, "history_recent_full_turns": 3}}


async def test_a_binding_without_a_companion_config_stores_null_and_reads_none(store):
    await store.adapter.save(binding())
    assert store.docs["C1"]["companion_config"] is None
    got = await store.adapter.get("C1")
    assert got == binding() and got.companion_config is None


async def test_save_overwrites_the_whole_binding_and_a_repeat_stores_the_same_document(store):
    await store.adapter.save(binding(companion_config=FULL))
    await store.adapter.save(binding(created_by="u2"))
    assert store.docs["C1"]["created_by"] == "u2" and store.docs["C1"]["companion_config"] is None
    first = dict(store.docs["C1"])
    await store.adapter.save(binding(created_by="u2"))
    assert store.docs["C1"] == first and list(store.docs) == ["C1"]


async def test_get_of_a_channel_that_is_not_bound_is_none(store):
    await store.adapter.save(binding("C1"))
    assert await store.adapter.get("C2") is None


async def test_delete_removes_the_binding_and_a_missing_one_is_a_no_op(store):
    await store.adapter.save(binding("C1"))
    await store.adapter.save(binding("C2"))
    await store.adapter.delete("C1")
    await store.adapter.delete("C1")
    await store.adapter.delete("C-never-bound")
    assert list(store.docs) == ["C2"]
    assert await store.adapter.get("C1") is None


async def test_a_partial_companion_config_takes_the_defaults_for_every_optional_field(store):
    raw_binding(store, {"window_threshold": 40, "batch_size": 25})
    config = (await store.adapter.get("C1")).companion_config
    assert config == CompanionConfig(window_threshold=40, batch_size=25)
    assert (config.text_mode, config.include_biographical, config.session_domains, config.include_standing_directives,
            config.include_own_records, config.history_recent_full_turns) == (CompanionTextMode.SUMMARY, False, [], False, True, 5)


async def test_a_binding_belongs_to_its_channel_not_to_the_user_who_made_it(store):
    await store.adapter.save(binding("C1", created_by="u1"))
    await store.adapter.save(binding("C2", created_by="u2"))
    first, second = await store.adapter.get("C1"), await store.adapter.get("C2")
    assert (first.created_by, second.created_by) == ("u1", "u2")
    # the store has no notion of the asking user: whoever reads C1 gets u1's binding, and anyone may overwrite it
    await store.adapter.save(binding("C1", created_by="u2"))
    assert (await store.adapter.get("C1")).created_by == "u2"
