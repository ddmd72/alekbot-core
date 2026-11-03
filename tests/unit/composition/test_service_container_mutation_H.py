"""Mutation-survivor kill for ServiceContainer: a missing optional prompt assembly module yields the (None, None) pair."""
from unittest.mock import MagicMock, patch

from src.composition.service_container import ServiceContainer


def test_a_missing_prompt_assembly_module_gives_a_pair_of_nones():
    with patch("src.composition.prompt_assembly_setup.build_prompt_assembly_service",
               side_effect=ImportError("not installed")):
        result = ServiceContainer._init_assembly_service({}, MagicMock(), MagicMock())

    assert result == (None, None)
