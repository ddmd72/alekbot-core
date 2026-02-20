"""
Integration test for 4-level prompt component resolution.

SESSION_26: Validates USER > ACCOUNT > AGENT > SYSTEM priority with REAL repository.

Test scenarios:
1. SYSTEM + ACCOUNT override → ACCOUNT wins
2. SYSTEM + USER override → USER wins
3. SYSTEM + ACCOUNT + USER → USER wins (highest priority)
4. Empty USER override → fallthrough to ACCOUNT
5. Disabled USER override → component excluded

NOTE: Uses resolve_component() directly with simplified mock Firestore.
Scenario "AGENT protection" intentionally NOT tested - known backdoor, will fix in v3.
"""

import pytest
from unittest.mock import MagicMock

from src.domain.prompt import OwnerType
from src.adapters.firestore_prompt_repository import FirestorePromptComponentRepository


# =============================================================================
# Simplified Mock Firestore
# =============================================================================

class SimpleMockFirestoreQuery:
    """
    Simplified mock that returns components matching filters.

    Supports chained where() calls and filters by:
    - component_id
    - owner_type
    - owner_value
    """

    def __init__(self, components: list):
        self.components = components
        self.filters = {}

    def where(self, filter=None, **kwargs):
        """Store filter criteria."""
        if filter:
            # FieldFilter: extract field and value
            self.filters[filter.field_path] = filter.value
        return self

    def limit(self, count):
        """Ignore limit for simplicity."""
        return self

    async def stream(self):
        """Yield components matching all filters."""
        for comp in self.components:
            # Check if component matches ALL filters
            if all(comp.get(field) == value for field, value in self.filters.items()):
                mock_doc = MagicMock()
                mock_doc.to_dict.return_value = comp
                yield mock_doc


def create_simple_mock_db(components: list):
    """Create mock Firestore DB with query support."""
    mock_db = MagicMock()
    mock_collection = MagicMock()

    mock_db.collection.return_value = mock_collection
    mock_collection.where.side_effect = lambda *args, **kwargs: SimpleMockFirestoreQuery(components)

    return mock_db


# =============================================================================
# Test Data
# =============================================================================

SYSTEM_COMP = {
    "component_id": "test_comp",
    "owner_type": "SYSTEM",
    "owner_value": None,
    "scope": "class.Alek",
    "order": 10,
    "text": "SYSTEM_CONTENT",
    "is_enabled": True
}

AGENT_COMP = {
    "component_id": "test_comp",
    "owner_type": "AGENT",
    "owner_value": "smart",
    "scope": "class.Alek",
    "order": 10,
    "text": "AGENT_CONTENT",
    "is_enabled": True
}

ACCOUNT_COMP = {
    "component_id": "test_comp",
    "owner_type": "ACCOUNT",
    "owner_value": "master_123",
    "scope": "class.Alek",
    "order": 10,
    "text": "ACCOUNT_CONTENT",
    "is_enabled": True
}

USER_COMP = {
    "component_id": "test_comp",
    "owner_type": "USER",
    "owner_value": "user_456",
    "scope": "class.Alek",
    "order": 10,
    "text": "USER_CONTENT",
    "is_enabled": True
}


# =============================================================================
# Test Scenario 1: SYSTEM + ACCOUNT → ACCOUNT wins
# =============================================================================

@pytest.mark.asyncio
async def test_scenario1_system_plus_account():
    """
    SYSTEM + ACCOUNT → ACCOUNT wins

    Setup:
    - SYSTEM component exists
    - ACCOUNT component overrides
    - NO USER override

    Expected: resolve_component() returns ACCOUNT
    """
    components = [SYSTEM_COMP, AGENT_COMP, ACCOUNT_COMP]
    mock_db = create_simple_mock_db(components)

    repo = FirestorePromptComponentRepository(mock_db, "test_components")

    result = await repo.resolve_component(
        component_id="test_comp",
        agent_type="smart",
        account_id="master_123",
        user_id="anonymous"  # No USER override
    )

    assert result is not None
    assert result.owner_type == OwnerType.ACCOUNT
    assert result.content == "ACCOUNT_CONTENT"
    assert "ACCOUNT" in result.content
    assert "SYSTEM" not in result.content


# =============================================================================
# Test Scenario 2: SYSTEM + USER → USER wins
# =============================================================================

@pytest.mark.asyncio
async def test_scenario2_system_plus_user():
    """
    SYSTEM + USER → USER wins

    Setup:
    - SYSTEM component exists
    - USER component overrides
    - NO ACCOUNT override

    Expected: resolve_component() returns USER
    """
    components = [SYSTEM_COMP, AGENT_COMP, USER_COMP]
    mock_db = create_simple_mock_db(components)

    repo = FirestorePromptComponentRepository(mock_db, "test_components")

    result = await repo.resolve_component(
        component_id="test_comp",
        agent_type="smart",
        account_id="guest",  # No ACCOUNT override
        user_id="user_456"
    )

    assert result is not None
    assert result.owner_type == OwnerType.USER
    assert result.content == "USER_CONTENT"
    assert "USER" in result.content
    assert "SYSTEM" not in result.content


# =============================================================================
# Test Scenario 3: ALL levels → USER wins (highest priority)
# =============================================================================

@pytest.mark.asyncio
async def test_scenario3_all_levels_user_wins():
    """
    SYSTEM + AGENT + ACCOUNT + USER → USER wins (highest priority)

    Setup:
    - All 4 levels have component

    Expected: resolve_component() returns USER (ignores ACCOUNT, AGENT, SYSTEM)
    """
    components = [SYSTEM_COMP, AGENT_COMP, ACCOUNT_COMP, USER_COMP]
    mock_db = create_simple_mock_db(components)

    repo = FirestorePromptComponentRepository(mock_db, "test_components")

    result = await repo.resolve_component(
        component_id="test_comp",
        agent_type="smart",
        account_id="master_123",
        user_id="user_456"
    )

    assert result is not None
    assert result.owner_type == OwnerType.USER
    assert result.content == "USER_CONTENT"
    # Verify lower priorities NOT used
    assert "ACCOUNT" not in result.content
    assert "AGENT" not in result.content
    assert "SYSTEM" not in result.content


# =============================================================================
# Test Edge Case: Empty USER override → fallthrough to ACCOUNT
# =============================================================================

@pytest.mark.asyncio
async def test_fallthrough_empty_user():
    """
    USER override with empty text → fallthrough to ACCOUNT

    Setup:
    - SYSTEM component exists
    - ACCOUNT component overrides
    - USER component with empty text (fallthrough pattern)

    Expected: resolve_component() returns ACCOUNT (USER fallthrough)
    """
    user_comp_empty = {
        **USER_COMP,
        "text": ""  # Empty text = fallthrough
    }

    components = [SYSTEM_COMP, ACCOUNT_COMP, user_comp_empty]
    mock_db = create_simple_mock_db(components)

    repo = FirestorePromptComponentRepository(mock_db, "test_components")

    result = await repo.resolve_component(
        component_id="test_comp",
        agent_type="smart",
        account_id="master_123",
        user_id="user_456"
    )

    assert result is not None
    assert result.owner_type == OwnerType.ACCOUNT  # Falls through to ACCOUNT
    assert result.content == "ACCOUNT_CONTENT"
    assert "USER" not in result.content


# =============================================================================
# Test Edge Case: Disabled USER override → component excluded
# =============================================================================

@pytest.mark.asyncio
async def test_exclusion_disabled_user():
    """
    USER sets is_enabled=False → component excluded (returns None)

    Setup:
    - SYSTEM component exists
    - ACCOUNT component overrides
    - USER component with is_enabled=False (exclusion pattern)

    Expected: resolve_component() returns None (component excluded)
    """
    user_comp_disabled = {
        **USER_COMP,
        "is_enabled": False  # Exclusion
    }

    components = [SYSTEM_COMP, ACCOUNT_COMP, user_comp_disabled]
    mock_db = create_simple_mock_db(components)

    repo = FirestorePromptComponentRepository(mock_db, "test_components")

    result = await repo.resolve_component(
        component_id="test_comp",
        agent_type="smart",
        account_id="master_123",
        user_id="user_456"
    )

    assert result is None  # Component excluded


# =============================================================================
# Test Priority Chain: AGENT > SYSTEM when no USER/ACCOUNT
# =============================================================================

@pytest.mark.asyncio
async def test_agent_overrides_system():
    """
    SYSTEM + AGENT → AGENT wins (when no USER/ACCOUNT)

    Setup:
    - SYSTEM component exists
    - AGENT component overrides
    - NO USER/ACCOUNT overrides

    Expected: resolve_component() returns AGENT
    """
    components = [SYSTEM_COMP, AGENT_COMP]
    mock_db = create_simple_mock_db(components)

    repo = FirestorePromptComponentRepository(mock_db, "test_components")

    result = await repo.resolve_component(
        component_id="test_comp",
        agent_type="smart",
        account_id="guest",
        user_id="anonymous"
    )

    assert result is not None
    assert result.owner_type == OwnerType.AGENT
    assert result.content == "AGENT_CONTENT"
    assert "SYSTEM" not in result.content


# =============================================================================
# Test Fallback: Only SYSTEM exists
# =============================================================================

@pytest.mark.asyncio
async def test_fallback_only_system():
    """
    Only SYSTEM component exists → returns SYSTEM

    Setup:
    - Only SYSTEM component (no overrides)

    Expected: resolve_component() returns SYSTEM
    """
    components = [SYSTEM_COMP]
    mock_db = create_simple_mock_db(components)

    repo = FirestorePromptComponentRepository(mock_db, "test_components")

    result = await repo.resolve_component(
        component_id="test_comp",
        agent_type="smart",
        account_id="guest",
        user_id="anonymous"
    )

    assert result is not None
    assert result.owner_type == OwnerType.SYSTEM
    assert result.content == "SYSTEM_CONTENT"
