"""
END-TO-END test for 4-level prompt assembly with REAL Firestore and REAL components.

SESSION_26: Validates USER > ACCOUNT > AGENT > SYSTEM priority resolution.

Flow:
1. Create REAL test components (SYSTEM, AGENT, ACCOUNT, USER)
2. Upload to Firestore (test_ collections)
3. Build prompt using REAL PromptBuilder
4. Verify assembled prompt contains correct overrides

Mock: Only the incoming Slack message
Real: Firestore, Repository, Service, Builder, assembled prompt
"""

import pytest
import os
from pathlib import Path

from google.cloud import firestore
from src.config.environment import EnvironmentConfig, Environment
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_component_service import PromptComponentService
from src.adapters.firestore_prompt_repository import FirestorePromptComponentRepository
from src.adapters.groovy_prompt_assembler import GroovyPromptAssembler
from src.ports.repository import FactRepository


# =============================================================================
# Test fixtures - Real Firestore connection
# =============================================================================

@pytest.fixture(scope="function")
async def real_firestore_db():
    """Real Firestore client connected to test environment."""
    # Force test environment (restore on exit so other tests are not affected)
    original_app_env = os.environ.get("APP_ENV")
    os.environ["APP_ENV"] = "test"
    env_config = EnvironmentConfig()
    assert env_config.env == Environment.TEST, "Must run in TEST environment"

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        if original_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = original_app_env
        pytest.skip("GOOGLE_CLOUD_PROJECT not set - cannot run Firestore integration test")

    db = firestore.AsyncClient(project=project_id)
    yield db
    # Restore APP_ENV so subsequent tests in the suite are not affected
    if original_app_env is None:
        os.environ.pop("APP_ENV", None)
    else:
        os.environ["APP_ENV"] = original_app_env


@pytest.fixture(scope="function")
async def prompt_repository(real_firestore_db):
    """Real FirestorePromptComponentRepository."""
    collection_name = "test_prompt_components"
    return FirestorePromptComponentRepository(real_firestore_db, collection_name)


@pytest.fixture(scope="function")
def prompt_service(prompt_repository):
    """Real PromptComponentService with GroovyAssembler."""
    assembler = GroovyPromptAssembler()
    return PromptComponentService(
        repository=prompt_repository,
        assembler=assembler,
        cache_ttl=0  # Disable cache for tests
    )


@pytest.fixture(scope="function")
async def mock_fact_repository():
    """Mock FactRepository (not testing facts, only prompts)."""
    from unittest.mock import AsyncMock
    mock_repo = AsyncMock(spec=FactRepository)
    mock_repo.get_biographical_context_cached.return_value = []
    mock_repo.get_active_facts.return_value = []
    return mock_repo


@pytest.fixture(scope="function")
def prompt_builder(mock_fact_repository, prompt_service):
    """Real PromptBuilder with component service."""
    return PromptBuilder(
        repo=mock_fact_repository,
        cache_ttl=0,  # Disable cache for tests
        assembly_service=prompt_service
    )


# =============================================================================
# Test data - Real component documents for Firestore
# =============================================================================

TEST_ACCOUNT_ID = "test_master_account"
TEST_USER_ID = "test_dev_user"

SYSTEM_PROPERTIES = {
    "component_id": "properties",
    "owner_type": "SYSTEM",
    "owner_value": None,
    "scope": "class.Alek.properties",
    "order": 20,
    "text": """properties {
    archetype: "SYSTEM_DEFAULT"
    vibe: "Default system vibe"
    humor_engine {
        status: "system_default"
    }
}
""",
    "is_enabled": True,
    "version": "1.0",
    "description": "System default properties"
}

AGENT_PROPERTIES = {
    "component_id": "properties",
    "owner_type": "AGENT",
    "owner_value": "smart",
    "scope": "class.Alek.properties",
    "order": 20,
    "text": """properties {
    archetype: "AGENT_SMART_OVERRIDE"
    vibe: "Smart agent specific"
    humor_engine {
        status: "agent_smart"
    }
}
""",
    "is_enabled": True,
    "version": "1.0",
    "description": "Smart agent override"
}

ACCOUNT_PROPERTIES = {
    "component_id": "properties",
    "owner_type": "ACCOUNT",
    "owner_value": TEST_ACCOUNT_ID,
    "scope": "class.Alek.properties",
    "order": 20,
    "text": """properties {
    archetype: "ACCOUNT_MASTER_OVERRIDE"
    vibe: "Master account vibe"
    humor_engine {
        status: "account_level"
        preset: "family_friendly"
    }
}
""",
    "is_enabled": True,
    "version": "1.0",
    "description": "Master account override"
}

USER_PROPERTIES = {
    "component_id": "properties",
    "owner_type": "USER",
    "owner_value": TEST_USER_ID,
    "scope": "class.Alek.properties",
    "order": 20,
    "text": """properties {
    archetype: "USER_DEV_OVERRIDE"
    vibe: "Dev user personal"
    humor_engine {
        status: "user_custom"
        preset: "ranevskaya"
    }
}
""",
    "is_enabled": True,
    "version": "1.0",
    "description": "Dev user personal override"
}


# =============================================================================
# Helper functions
# =============================================================================

async def _upload_component(db: firestore.AsyncClient, collection_name: str, component: dict):
    """Upload single component to Firestore."""
    collection = db.collection(collection_name)

    # Check if exists
    query = (
        collection
        .where(filter=firestore.FieldFilter("component_id", "==", component["component_id"]))
        .where(filter=firestore.FieldFilter("owner_type", "==", component["owner_type"]))
        .where(filter=firestore.FieldFilter("owner_value", "==", component["owner_value"]))
        .limit(1)
    )

    docs = [doc async for doc in query.stream()]

    if docs:
        # Update existing
        await docs[0].reference.set(component)
    else:
        # Create new
        await collection.document().set(component)


async def _cleanup_test_components(db: firestore.AsyncClient, collection_prefix: str):
    """Clean up test components after tests."""
    collection_name = f"{collection_prefix}prompt_components"
    collection = db.collection(collection_name)

    # Delete test account components
    query = collection.where(
        filter=firestore.FieldFilter("owner_value", "==", TEST_ACCOUNT_ID)
    )
    docs = [doc async for doc in query.stream()]
    for doc in docs:
        await doc.reference.delete()

    # Delete test user components
    query = collection.where(
        filter=firestore.FieldFilter("owner_value", "==", TEST_USER_ID)
    )
    docs = [doc async for doc in query.stream()]
    for doc in docs:
        await doc.reference.delete()


# =============================================================================
# Test Scenario 1: SYSTEM + ACCOUNT → ACCOUNT wins
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skip(reason="Written for v3 PromptComponentService; needs rewrite for v4 PromptAssemblyService (token/blueprint system)")
async def test_e2e_system_plus_account(real_firestore_db, prompt_builder):
    """
    E2E: SYSTEM + ACCOUNT → ACCOUNT wins

    Setup:
    - Upload SYSTEM properties
    - Upload ACCOUNT properties
    - NO USER properties

    Flow:
    - Call prompt_builder.build_for_agent() with account_id
    - Verify assembled prompt contains ACCOUNT content
    """
    # Upload components
    await _upload_component(real_firestore_db, "test_prompt_components", SYSTEM_PROPERTIES)
    await _upload_component(real_firestore_db, "test_prompt_components", ACCOUNT_PROPERTIES)

    # Build prompt (REAL call through entire stack)
    assembled = await prompt_builder.build_for_agent(
        agent_type="smart",
        user_id="anonymous",  # No USER override
        account_id=TEST_ACCOUNT_ID,
        routing_metadata=None,
        semantic_context=""
    )

    # Verify ACCOUNT override wins
    assert "ACCOUNT_MASTER_OVERRIDE" in assembled, "ACCOUNT archetype should be present"
    assert "family_friendly" in assembled, "ACCOUNT humor preset should be present"

    # Verify SYSTEM default NOT present
    assert "SYSTEM_DEFAULT" not in assembled, "SYSTEM should be overridden"


# =============================================================================
# Test Scenario 2: SYSTEM + USER → USER wins
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skip(reason="Written for v3 PromptComponentService; needs rewrite for v4 PromptAssemblyService (token/blueprint system)")
async def test_e2e_system_plus_user(real_firestore_db, prompt_builder):
    """
    E2E: SYSTEM + USER → USER wins

    Setup:
    - Upload SYSTEM properties
    - Upload USER properties
    - NO ACCOUNT properties

    Flow:
    - Call prompt_builder.build_for_agent() with user_id
    - Verify assembled prompt contains USER content
    """
    # Upload components
    await _upload_component(real_firestore_db, "test_prompt_components", SYSTEM_PROPERTIES)
    await _upload_component(real_firestore_db, "test_prompt_components", USER_PROPERTIES)

    # Build prompt (REAL call)
    assembled = await prompt_builder.build_for_agent(
        agent_type="smart",
        user_id=TEST_USER_ID,
        account_id="guest",  # No ACCOUNT override
        routing_metadata=None,
        semantic_context=""
    )

    # Verify USER override wins
    assert "USER_DEV_OVERRIDE" in assembled, "USER archetype should be present"
    assert "ranevskaya" in assembled, "USER humor preset should be present"

    # Verify SYSTEM default NOT present
    assert "SYSTEM_DEFAULT" not in assembled, "SYSTEM should be overridden"


# =============================================================================
# Test Scenario 3: ALL levels → USER wins (highest priority)
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skip(reason="Written for v3 PromptComponentService; needs rewrite for v4 PromptAssemblyService (token/blueprint system)")
async def test_e2e_all_levels_user_wins(real_firestore_db, prompt_builder):
    """
    E2E: SYSTEM + AGENT + ACCOUNT + USER → USER wins

    Setup:
    - Upload ALL 4 levels of properties

    Flow:
    - Call prompt_builder.build_for_agent() with user_id + account_id
    - Verify assembled prompt contains ONLY USER content (highest priority)
    """
    # Upload ALL components
    await _upload_component(real_firestore_db, "test_prompt_components", SYSTEM_PROPERTIES)
    await _upload_component(real_firestore_db, "test_prompt_components", AGENT_PROPERTIES)
    await _upload_component(real_firestore_db, "test_prompt_components", ACCOUNT_PROPERTIES)
    await _upload_component(real_firestore_db, "test_prompt_components", USER_PROPERTIES)

    # Build prompt (REAL call)
    assembled = await prompt_builder.build_for_agent(
        agent_type="smart",
        user_id=TEST_USER_ID,
        account_id=TEST_ACCOUNT_ID,
        routing_metadata=None,
        semantic_context=""
    )

    # Verify USER override wins (highest priority)
    assert "USER_DEV_OVERRIDE" in assembled, "USER archetype should be present"
    assert "ranevskaya" in assembled, "USER humor preset should be present"

    # Verify lower priorities NOT present
    assert "ACCOUNT_MASTER_OVERRIDE" not in assembled, "ACCOUNT should be overridden by USER"
    assert "AGENT_SMART_OVERRIDE" not in assembled, "AGENT should be overridden by USER"
    assert "SYSTEM_DEFAULT" not in assembled, "SYSTEM should be overridden by USER"
