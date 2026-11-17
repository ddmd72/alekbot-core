"""The dry-run kit's overlay store is also the pass's cache refresher: a refresh or a cache save through it is recorded and
never reaches the live cache document."""
from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.ports.biographical_cache_port import BiographicalCacheRefresher
from tests.support.fact_kit import ACC
from tests.support.fact_store import FactStore, make_env_config

CACHE = make_env_config().user_context_collection


async def test_a_refresh_and_a_save_are_recorded_and_the_live_cache_is_untouched():
    store = FactStore()
    kit = NoWriteFactRepository(store.repo)

    assert isinstance(kit, BiographicalCacheRefresher)
    await kit.refresh_cache(ACC, facts_limit=3, principles_limit=1)
    await kit.save_biographical_context_cache(ACC, {"facts": [], "principles": []})

    assert kit.writes == [{"kind": "refresh_biographical_context_cache"}, {"kind": "save_biographical_context_cache"}]
    assert store.db.collection(CACHE).docs == {}
