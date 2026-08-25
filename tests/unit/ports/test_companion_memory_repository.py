import pytest

from src.ports.companion_memory_repository import CompanionMemoryRepository


def test_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        CompanionMemoryRepository()


def test_concrete_implementation_must_implement_both_methods():
    class Incomplete(CompanionMemoryRepository):
        async def save_batch(self, records):
            return 0
        # find_nearest missing

    with pytest.raises(TypeError):
        Incomplete()
