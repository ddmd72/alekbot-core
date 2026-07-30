"""
Port contract tests for RecurrencePort.

Covers:
- RecurrencePort (4 abstract sync methods: normalize, first_occurrence,
  next_occurrence, describe)
- Methods are synchronous by design — pure computation, no I/O
- MagicMock(spec=RecurrencePort) satisfies the port contract in agent/service tests
"""

from abc import ABC
import inspect

import pytest
from unittest.mock import MagicMock

from src.ports.recurrence_port import RecurrencePort


class TestRecurrencePortContract:

    def test_is_abstract_class(self):
        assert issubclass(RecurrencePort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            RecurrencePort()

    @pytest.mark.parametrize(
        "method", ["normalize", "first_occurrence", "next_occurrence", "describe"]
    )
    def test_declares_abstract_method(self, method):
        assert getattr(getattr(RecurrencePort, method), "__isabstractmethod__", False)

    @pytest.mark.parametrize(
        "method", ["normalize", "first_occurrence", "next_occurrence", "describe"]
    )
    def test_methods_are_synchronous(self, method):
        """Rule evaluation is CPU-only. An async signature here would force every
        caller — including prompt rendering — into an await for no I/O."""
        assert not inspect.iscoroutinefunction(getattr(RecurrencePort, method))

    def test_spec_mock_satisfies_the_contract(self):
        mock = MagicMock(spec=RecurrencePort)
        mock.normalize.return_value = "FREQ=DAILY"
        assert mock.normalize("freq=daily") == "FREQ=DAILY"
        with pytest.raises(AttributeError):
            mock.expand_all()
