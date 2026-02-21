import pytest
from unittest.mock import MagicMock
from src.adapters.groovy_prompt_assembler import GroovyPromptAssembler
from src.adapters.firestore_prompt_repository import FirestorePromptComponentRepository
from src.services.prompt_component_service import PromptComponentService
from src.domain.prompt import PromptTemplate, ComponentScope, ANONYMOUS_USER_ID, ANONYMOUS_ACCOUNT_ID

# =============================================================================
# Test data: Real prompt components for all 4 levels
# =============================================================================

# SYSTEM level (default for all agents)
SYSTEM_KERNEL = {
    "component_id": "kernel",
    "owner_type": "SYSTEM",
    "owner_value": None,
    "scope": "class.Alek",
    "order": 10,
    "text": """cognitive_process {
    steps: [
        "1. ANALYZE: Parse user query",
        "2. SYSTEM_DEFAULT: Use system reasoning"
    ]
}""",
    "is_enabled": True,
    "version": "1.0"
}

SYSTEM_PROPERTIES = {
    "component_id": "properties",
    "owner_type": "SYSTEM",
    "owner_value": None,
    "scope": "class.Alek.properties",
    "order": 20,
    "text": """humor_engine {
    status: "SYSTEM_DEFAULT"
    preset: "neutral"
}""",
    "is_enabled": True,
    "version": "1.0"
}

# AGENT level (smart agent specific)
AGENT_KERNEL = {
    "component_id": "kernel",
    "owner_type": "AGENT",
    "owner_value": "smart",
    "scope": "class.Alek",
    "order": 10,
    "text": """cognitive_process {
    steps: [
        "1. ANALYZE: Parse user query",
        "2. AGENT_SMART: Use advanced reasoning with tools"
    ]
}""",
    "is_enabled": True,
    "version": "1.0"
}

# ACCOUNT level (master account / family plan)
ACCOUNT_PROPERTIES = {
    "component_id": "properties",
    "owner_type": "ACCOUNT",
    "owner_value": "master_account_123",
    "scope": "class.Alek.properties",
    "order": 20,
    "text": """humor_engine {
    status: "ACCOUNT_OVERRIDE"
    preset: "family_friendly"
}""",
    "is_enabled": True,
    "version": "1.0"
}

# USER level (personal customization)
USER_PROPERTIES = {
    "component_id": "properties",
    "owner_type": "USER",
    "owner_value": "dev_user_456",
    "scope": "class.Alek.properties",
    "order": 20,
    "text": """humor_engine {
    status: "USER_OVERRIDE"
    preset: "ranevskaya"
}""",
    "is_enabled": True,
    "version": "1.0"
}


# =============================================================================
# Mock Firestore setup
# =============================================================================

def create_mock_firestore_with_data(components: list):
    """
    Create mock Firestore client with test components.

    Args:
        components: List of component dicts to return from queries

    Returns:
        Mock Firestore client
    """
    mock_db = MagicMock()
    mock_collection = MagicMock()

    # Mock collection() to return mock_collection
    mock_db.collection.return_value = mock_collection

    # Store filters applied via where() calls
    class MockQuery:
        def __init__(self):
            self.filters = {}

        def where(self, filter=None, **kwargs):
            # Extract filter from FieldFilter or kwargs
            if filter:
                # FieldFilter object: filter.field_path, filter.op_string, filter.value
                field = filter.field_path
                value = filter.value
                self.filters[field] = value
            return self

        def limit(self, count):
            return self

        async def stream(self):
            # Filter components based on accumulated filters
            for comp_data in components:
                # Check all filters
                matches = True
                for field, expected_value in self.filters.items():
                    actual_value = comp_data.get(field)
                    if actual_value != expected_value:
                        matches = False
                        break

                if matches:
                    mock_doc = MagicMock()
                    mock_doc.to_dict.return_value = comp_data
                    mock_doc.id = f"{comp_data['component_id']}_{comp_data['owner_type']}"
                    yield mock_doc

    # Mock where() to return MockQuery, capturing the initial filter
    def create_mock_query(*args, **kwargs):
        q = MockQuery()
        filter_obj = kwargs.get('filter') or (args[0] if args else None)
        if filter_obj and hasattr(filter_obj, 'field_path'):
            q.filters[filter_obj.field_path] = filter_obj.value
        return q

    mock_collection.where.side_effect = create_mock_query

    return mock_db


# =============================================================================
# Test fixtures
# =============================================================================

@pytest.fixture
def mock_assembler():
    """Real Groovy assembler (not mocked)."""
    return GroovyPromptAssembler()


@pytest.fixture
def test_template():
    """Simplified test template (just properties + kernel)."""
    return PromptTemplate(
        name="AlekTest",
        extends="Agent",
        scopes=[
            ComponentScope.CLASS_ROOT,       # kernel (cognitive_process)
            ComponentScope.CLASS_PROPERTIES  # properties (humor_engine)
        ],
        supports_tools=False
    )


# =============================================================================
# Test Scenario 1: SYSTEM + ACCOUNT → ACCOUNT wins
# =============================================================================

@pytest.mark.asyncio
async def test_scenario1_system_plus_account(mock_assembler, test_template):
    """
    Test: SYSTEM + ACCOUNT override → ACCOUNT wins

    Setup:
    - SYSTEM: kernel (cognitive_process), properties (humor_engine="SYSTEM_DEFAULT")
    - ACCOUNT: properties override (humor_engine="ACCOUNT_OVERRIDE")
    - NO USER override

    Expected:
    - kernel: SYSTEM (no override)
    - properties: ACCOUNT (overrides SYSTEM)
    """
    # Setup mock Firestore with SYSTEM + ACCOUNT components
    components = [
        SYSTEM_KERNEL,
        SYSTEM_PROPERTIES,
        ACCOUNT_PROPERTIES  # Account overrides properties
    ]

    mock_db = create_mock_firestore_with_data(components)

    # Create repository and service with REAL implementations
    repository = FirestorePromptComponentRepository(
        db_client=mock_db,
        collection_name="test_prompt_components"
    )

    service = PromptComponentService(
        repository=repository,
        assembler=mock_assembler
    )

    # Assemble prompt for master account (no user override)
    assembled = await service.get_assembled_prompt(
        template=test_template,
        agent_type="smart",
        user_id=ANONYMOUS_USER_ID,  # Anonymous user (no USER override)
        account_id="master_account_123"
    )

    # Verify: ACCOUNT override wins for properties
    assert "ACCOUNT_OVERRIDE" in assembled
    assert "family_friendly" in assembled

    # Verify: SYSTEM wins for kernel (no override)
    assert "SYSTEM_DEFAULT" in assembled or "ANALYZE" in assembled

    # Verify: USER override NOT present
    assert "USER_OVERRIDE" not in assembled
    assert "ranevskaya" not in assembled


# =============================================================================
# Test Scenario 2: SYSTEM + USER → USER wins
# =============================================================================

@pytest.mark.asyncio
async def test_scenario2_system_plus_user(mock_assembler, test_template):
    """
    Test: SYSTEM + USER override → USER wins

    Setup:
    - SYSTEM: kernel, properties (humor_engine="SYSTEM_DEFAULT")
    - USER: properties override (humor_engine="USER_OVERRIDE")
    - NO ACCOUNT override

    Expected:
    - kernel: SYSTEM (no override)
    - properties: USER (overrides SYSTEM)
    """
    # Setup mock Firestore with SYSTEM + USER components
    components = [
        SYSTEM_KERNEL,
        SYSTEM_PROPERTIES,
        USER_PROPERTIES  # User overrides properties
    ]

    mock_db = create_mock_firestore_with_data(components)

    repository = FirestorePromptComponentRepository(
        db_client=mock_db,
        collection_name="test_prompt_components"
    )

    service = PromptComponentService(
        repository=repository,
        assembler=mock_assembler
    )

    # Assemble prompt for dev user (no account override)
    assembled = await service.get_assembled_prompt(
        template=test_template,
        agent_type="smart",
        user_id="dev_user_456",
        account_id=ANONYMOUS_ACCOUNT_ID  # Anonymous account (no ACCOUNT override)
    )

    # Verify: USER override wins for properties
    assert "USER_OVERRIDE" in assembled
    assert "ranevskaya" in assembled

    # Verify: SYSTEM wins for kernel (no override)
    assert "ANALYZE" in assembled

    # Verify: ACCOUNT override NOT present
    assert "ACCOUNT_OVERRIDE" not in assembled
    assert "family_friendly" not in assembled


# =============================================================================
# Test Scenario 3: SYSTEM + ACCOUNT + USER → USER wins (highest priority)
# =============================================================================

@pytest.mark.asyncio
async def test_scenario3_all_levels_user_wins(mock_assembler, test_template):
    """
    Test: SYSTEM + ACCOUNT + USER → USER wins (highest priority)

    Setup:
    - SYSTEM: kernel, properties (humor_engine="SYSTEM_DEFAULT")
    - ACCOUNT: properties override (humor_engine="ACCOUNT_OVERRIDE")
    - USER: properties override (humor_engine="USER_OVERRIDE")

    Expected:
    - kernel: SYSTEM (no override)
    - properties: USER (highest priority, overrides both SYSTEM and ACCOUNT)
    """
    # Setup mock Firestore with ALL levels
    components = [
        SYSTEM_KERNEL,
        SYSTEM_PROPERTIES,
        ACCOUNT_PROPERTIES,  # Account tries to override
        USER_PROPERTIES      # User override wins
    ]

    mock_db = create_mock_firestore_with_data(components)

    repository = FirestorePromptComponentRepository(
        db_client=mock_db,
        collection_name="test_prompt_components"
    )

    service = PromptComponentService(
        repository=repository,
        assembler=mock_assembler
    )

    # Assemble prompt for dev user with master account
    assembled = await service.get_assembled_prompt(
        template=test_template,
        agent_type="smart",
        user_id="dev_user_456",
        account_id="master_account_123"
    )

    # Verify: USER override wins (highest priority)
    assert "USER_OVERRIDE" in assembled
    assert "ranevskaya" in assembled

    # Verify: ACCOUNT override NOT present (USER overrides it)
    assert "ACCOUNT_OVERRIDE" not in assembled
    assert "family_friendly" not in assembled

    # Verify: SYSTEM wins for kernel (no override at any level)
    assert "ANALYZE" in assembled


# =============================================================================
# Test edge case: Empty content fallthrough
# =============================================================================

@pytest.mark.asyncio
async def test_fallthrough_empty_user_override(mock_assembler, test_template):
    """
    Test: USER override with empty text → fallthrough to ACCOUNT

    Setup:
    - SYSTEM: properties (humor_engine="SYSTEM_DEFAULT")
    - ACCOUNT: properties override (humor_engine="ACCOUNT_OVERRIDE")
    - USER: properties override with EMPTY text (fallthrough)

    Expected:
    - properties: ACCOUNT (USER fallthrough leads to ACCOUNT)
    """
    # USER override with EMPTY text (fallthrough pattern)
    user_properties_empty = {
        "component_id": "properties",
        "owner_type": "USER",
        "owner_value": "dev_user_456",
        "scope": "class.Alek.properties",
        "order": 20,
        "text": "",  # Empty = fallthrough
        "is_enabled": True,
        "version": "1.0"
    }

    components = [
        SYSTEM_KERNEL,
        SYSTEM_PROPERTIES,
        ACCOUNT_PROPERTIES,
        user_properties_empty  # User fallthrough
    ]

    mock_db = create_mock_firestore_with_data(components)

    repository = FirestorePromptComponentRepository(
        db_client=mock_db,
        collection_name="test_prompt_components"
    )

    service = PromptComponentService(
        repository=repository,
        assembler=mock_assembler
    )

    assembled = await service.get_assembled_prompt(
        template=test_template,
        agent_type="smart",
        user_id="dev_user_456",
        account_id="master_account_123"
    )

    # Verify: ACCOUNT wins (USER fallthrough)
    assert "ACCOUNT_OVERRIDE" in assembled
    assert "family_friendly" in assembled

    # Verify: USER override NOT present (empty fallthrough)
    assert "USER_OVERRIDE" not in assembled


# =============================================================================
# Test edge case: Component exclusion (is_enabled=False)
# =============================================================================

@pytest.mark.asyncio
async def test_exclusion_user_disables_component(mock_assembler, test_template):
    """
    Test: USER sets is_enabled=False → component EXCLUDED from assembly

    Setup:
    - SYSTEM: properties (humor_engine="SYSTEM_DEFAULT")
    - ACCOUNT: properties override (humor_engine="ACCOUNT_OVERRIDE")
    - USER: properties with is_enabled=False (EXCLUSION)

    Expected:
    - properties: NOT present in assembled output (excluded)
    """
    # USER override with is_enabled=False (exclusion pattern)
    user_properties_disabled = {
        "component_id": "properties",
        "owner_type": "USER",
        "owner_value": "dev_user_456",
        "scope": "class.Alek.properties",
        "order": 20,
        "text": "placeholder",
        "is_enabled": False,  # EXCLUSION
        "version": "1.0"
    }

    components = [
        SYSTEM_KERNEL,
        SYSTEM_PROPERTIES,
        ACCOUNT_PROPERTIES,
        user_properties_disabled  # User excludes component
    ]

    mock_db = create_mock_firestore_with_data(components)

    repository = FirestorePromptComponentRepository(
        db_client=mock_db,
        collection_name="test_prompt_components"
    )

    service = PromptComponentService(
        repository=repository,
        assembler=mock_assembler
    )

    assembled = await service.get_assembled_prompt(
        template=test_template,
        agent_type="smart",
        user_id="dev_user_456",
        account_id="master_account_123"
    )

    # Verify: properties component EXCLUDED (kernel still present)
    assert "humor_engine" not in assembled
    assert "ACCOUNT_OVERRIDE" not in assembled
    assert "USER_OVERRIDE" not in assembled
    assert "ANALYZE" in assembled
