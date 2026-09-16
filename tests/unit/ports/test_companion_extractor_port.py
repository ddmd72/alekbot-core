import pytest

from src.ports.companion_extractor_port import CompanionExtractorPort


def test_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        CompanionExtractorPort()


def test_concrete_implementation_must_implement_extract():
    class Incomplete(CompanionExtractorPort):
        pass

    with pytest.raises(TypeError):
        Incomplete()
