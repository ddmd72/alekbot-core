"""Mutation-survivor kill for ServiceContainer: a failing prompt assembly build yields the (None, None) pair."""
from unittest.mock import MagicMock, patch

from src.composition.service_container import ServiceContainer


def test_a_failing_prompt_assembly_build_gives_a_pair_of_nones():
    with patch("src.composition.prompt_assembly_setup.build_prompt_assembly_service",
               side_effect=RuntimeError("firestore down")):
        result = ServiceContainer._init_assembly_service({}, MagicMock(), MagicMock())

    assert result == (None, None)
