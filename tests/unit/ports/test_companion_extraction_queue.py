import pytest

from src.ports.companion_extraction_queue import CompanionExtractionQueue


def test_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        CompanionExtractionQueue()


def test_concrete_implementation_must_implement_all_methods():
    class Incomplete(CompanionExtractionQueue):
        async def enqueue_batch(self, batch):
            return "id"
        # remaining abstract methods missing

    with pytest.raises(TypeError):
        Incomplete()
