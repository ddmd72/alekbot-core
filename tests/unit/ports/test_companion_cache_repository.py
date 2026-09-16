import pytest

from src.ports.companion_cache_repository import CompanionCacheRepository


def test_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        CompanionCacheRepository()


def test_concrete_implementation_must_implement_both_methods():
    class Incomplete(CompanionCacheRepository):
        async def get_summary(self, session_id):
            return None
        # save_summary missing

    with pytest.raises(TypeError):
        Incomplete()
