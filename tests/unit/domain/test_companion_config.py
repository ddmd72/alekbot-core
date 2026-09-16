import dataclasses

import pytest

from src.domain.companion_config import CompanionConfig, CompanionTextMode
from src.domain.entities import FactDomain


def test_minimal_construction_defaults():
    config = CompanionConfig(window_threshold=100, batch_size=50)
    assert config.window_threshold == 100
    assert config.batch_size == 50
    assert config.text_mode == CompanionTextMode.SUMMARY
    assert config.include_biographical is False
    assert config.session_domains == []
    assert config.include_standing_directives is False
    assert config.include_own_records is True


def test_full_construction():
    config = CompanionConfig(
        window_threshold=100,
        batch_size=50,
        text_mode=CompanionTextMode.FULL,
        include_biographical=True,
        session_domains=[FactDomain.EDUCATION, FactDomain.SKILL],
        include_standing_directives=True,
        include_own_records=False,
    )
    assert config.text_mode == CompanionTextMode.FULL
    assert config.include_biographical is True
    assert config.session_domains == [FactDomain.EDUCATION, FactDomain.SKILL]
    assert config.include_standing_directives is True
    assert config.include_own_records is False


def test_companion_text_mode_string_values():
    assert CompanionTextMode.SUMMARY.value == "summary"
    assert CompanionTextMode.FULL.value == "full"


def test_frozen():
    config = CompanionConfig(window_threshold=100, batch_size=50)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.window_threshold = 200
