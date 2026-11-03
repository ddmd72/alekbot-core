"""TranscriptCachePort kit (P11; EXTERNAL_FILES_RFC §4.4 P21): the docstring of `src/ports/transcript_cache_port.py`
checked against `FirestoreTranscriptCache` on the evaluating Firestore fake (H1), with the index refusal on (the store
makes no query). Assertions are on the raw stored documents and on what `get` returns."""
from datetime import timedelta, timezone
from pathlib import Path

import pytest

import src.adapters.firestore_transcript_cache as cache_module
from src.adapters.firestore_transcript_cache import FirestoreTranscriptCache
from src.config.environment import EnvironmentConfig
from src.ports.transcript_cache_port import TranscriptCachePort
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COLLECTION = "eval_audio_transcripts"
T0 = UTC_MIDNIGHT.utc
KEY = "3f2a9c:gpt-transcribe"          # content hash + model, built by the caller
LINE = "Lukas Weber owns the Q3 budget."
LIFE = timedelta(days=90)


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={COLLECTION: "development_audio_transcripts"})
        self.cache: TranscriptCachePort = FirestoreTranscriptCache(self.db, COLLECTION)

    @property
    def docs(self) -> dict:
        return self.db.collection(COLLECTION).docs


@pytest.fixture
def store(monkeypatch):
    pin_datetime(monkeypatch, cache_module, T0)
    return Store()


def at(monkeypatch, delta: timedelta) -> None:
    pin_datetime(monkeypatch, cache_module, T0 + delta)


def test_the_adapter_implements_the_port():
    assert issubclass(FirestoreTranscriptCache, TranscriptCachePort)


def test_the_collection_is_env_prefixed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    assert EnvironmentConfig().audio_transcripts_collection == "development_audio_transcripts"


async def test_a_put_stores_the_four_fields_under_account_and_key(store):
    await store.cache.put("ann", KEY, LINE, purge_after=T0 + LIFE)

    assert set(store.docs) == {f"ann:{KEY}"}
    assert store.docs[f"ann:{KEY}"] == {"text": LINE, "account_id": "ann", "written_at": T0, "purge_after": T0 + LIFE}


async def test_a_get_reads_back_the_text(store):
    await store.cache.put("ann", KEY, LINE, purge_after=T0 + LIFE)
    assert await store.cache.get("ann", KEY) == LINE


async def test_an_unknown_key_is_none(store):
    assert await store.cache.get("ann", KEY) is None


async def test_entries_are_per_account(store):
    await store.cache.put("ann", KEY, LINE, purge_after=T0 + LIFE)
    assert await store.cache.get("bob", KEY) is None


async def test_a_second_put_replaces_the_entry(store, monkeypatch):
    await store.cache.put("ann", KEY, "draft", purge_after=T0 + LIFE)
    at(monkeypatch, timedelta(days=1))
    await store.cache.put("ann", KEY, LINE, purge_after=T0 + LIFE + timedelta(days=1))

    assert await store.cache.get("ann", KEY) == LINE
    assert store.docs[f"ann:{KEY}"]["written_at"] == T0 + timedelta(days=1)


async def test_an_entry_past_purge_after_is_absent_before_the_ttl_removes_it(store, monkeypatch):
    await store.cache.put("ann", KEY, LINE, purge_after=T0 + LIFE)

    at(monkeypatch, LIFE - timedelta(seconds=1))
    assert await store.cache.get("ann", KEY) == LINE
    at(monkeypatch, LIFE)
    assert await store.cache.get("ann", KEY) is None
    assert f"ann:{KEY}" in store.docs                     # the TTL lags; the read never deletes


async def test_purge_after_compares_as_an_instant(store, monkeypatch):
    """An aware `purge_after` in another zone is the same instant."""
    berlin = timezone(timedelta(hours=1))
    await store.cache.put("ann", KEY, LINE, purge_after=(T0 + LIFE).astimezone(berlin))
    at(monkeypatch, LIFE - timedelta(seconds=1))
    assert await store.cache.get("ann", KEY) == LINE


async def test_a_naive_purge_after_is_refused(store):
    with pytest.raises(ValueError):
        await store.cache.put("ann", KEY, LINE, purge_after=(T0 + LIFE).replace(tzinfo=None))
    assert store.docs == {}


@pytest.mark.parametrize("account_id,key", [("", KEY), ("ann", ""), ("a/b", KEY), ("ann", "x/y")])
async def test_an_empty_or_pathlike_address_is_refused(store, account_id, key):
    with pytest.raises(ValueError):
        await store.cache.put(account_id, key, LINE, purge_after=T0 + LIFE)
    with pytest.raises(ValueError):
        await store.cache.get(account_id, key)
    assert store.docs == {}


async def test_an_entry_without_text_or_purge_after_is_absent(store):
    await store.db.collection(COLLECTION).document(f"ann:{KEY}").set({"account_id": "ann", "purge_after": T0 + LIFE})
    await store.db.collection(COLLECTION).document("ann:other").set({"account_id": "ann", "text": LINE})
    assert await store.cache.get("ann", KEY) is None
    assert await store.cache.get("ann", "other") is None
