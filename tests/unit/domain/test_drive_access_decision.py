"""`AccessDecision` (the answer of `drive_access_policy.decide`) and the `ALLOWED` singleton."""
import dataclasses

import pytest

from src.domain.drive_access_decision import ALLOWED, AccessDecision


def test_allowed_is_an_allowed_decision_with_no_refusal_line():
    assert ALLOWED.allowed is True
    assert ALLOWED.line == ""


def test_allowed_equals_a_fresh_allowing_decision():
    assert ALLOWED == AccessDecision(True)


def test_a_refusal_carries_its_line_verbatim():
    decision = AccessDecision(False, "No access to this drive.")
    assert decision.allowed is False
    assert decision.line == "No access to this drive."


def test_the_line_defaults_to_empty_when_only_allowed_is_given():
    assert AccessDecision(False).line == ""


def test_a_refusal_differs_from_allowed():
    assert AccessDecision(False, "no") != ALLOWED


def test_a_decision_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ALLOWED.allowed = False  # type: ignore[misc]
