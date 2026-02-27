"""Unit tests for OwnerType enum (v4).

v4 changes:
- BlueprintClass removed (no more allowed_token_categories, overridable_by).
  Override matching is now done by class+category on token documents.
- OwnerType simplified to 3 values: AGENT / ACCOUNT / USER.
  SYSTEM was merged into AGENT.
"""

from src.domain.prompt_v3.slot import OwnerType


def test_owner_type_has_exactly_3_values():
    """OwnerType v4 has exactly 3 levels (SYSTEM merged into AGENT)."""
    assert len(OwnerType) == 3


def test_owner_type_values():
    """OwnerType enum values are correct."""
    assert OwnerType.AGENT.value == "agent"
    assert OwnerType.ACCOUNT.value == "account"
    assert OwnerType.USER.value == "user"


def test_owner_type_lookup_by_name():
    """OwnerType can be looked up by name."""
    assert OwnerType["AGENT"] == OwnerType.AGENT
    assert OwnerType["ACCOUNT"] == OwnerType.ACCOUNT
    assert OwnerType["USER"] == OwnerType.USER


def test_owner_type_lookup_by_value():
    """OwnerType can be looked up by value string."""
    assert OwnerType("agent") == OwnerType.AGENT
    assert OwnerType("account") == OwnerType.ACCOUNT
    assert OwnerType("user") == OwnerType.USER
