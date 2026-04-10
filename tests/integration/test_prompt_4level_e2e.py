"""
END-TO-END test for 3-level prompt override with REAL Firestore and v4 PromptAssemblyService.

Rewritten for v4 (token/blueprint/profile system).
Validates USER > ACCOUNT > AGENT priority resolution.

v4 changes from v3:
- SYSTEM level merged into AGENT — only 3 levels remain
- Override semantics: class+category match (not whole-component replacement)
- non_overridable flag blocks account/user overrides

Flow:
1. Upload test tokens, blueprint, profile, and overrides to Firestore test collections
2. Build PromptAssemblyService with REAL Firestore adapters
3. Call assemble() and verify correct override behaviour

Mock: SecurityPort (pass-through), BiographicalFactsFormatter (empty)
Real: Firestore, TokenRepository, BlueprintRepository, AgentProfileRepository, PromptAssemblyService
"""

import pytest
import os

from google.cloud import firestore

from src.config.environment import EnvironmentConfig, Environment
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter
from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.ports.security_port import SecurityPort, ValidationResult, RiskLevel, TrustZone


# =============================================================================
# Constants
# =============================================================================

TEST_ACCOUNT_ID = "test_integ_account"
TEST_USER_ID = "test_integ_user"

# Collection names — isolated test collections, cleaned up after each test
_COL_TOKENS = "test_integ_prompt_tokens"
_COL_BLUEPRINTS = "test_integ_prompt_blueprints"
_COL_PROFILES = "test_integ_prompt_profiles"
_COL_OVERRIDES = "test_integ_prompt_overrides"


# =============================================================================
# Mock SecurityPort (pass-through)
# =============================================================================

class _PassthroughSecurityPort(SecurityPort):
    async def validate(self, text, context=None, zone=TrustZone.UNTRUSTED):
        return ValidationResult(
            sanitized_text=text,
            risk_level=RiskLevel.SAFE,
            risk_score=0.0,
            patterns_detected=[],
            action_taken="passed",
            metadata={"adapter": "test_passthrough"},
        )


# =============================================================================
# Test data — v4 token/blueprint/profile documents
# =============================================================================

# Tokens: 3 humor tokens (agent/account/user) + 1 cognitive process token
_TOKENS = {
    "TEST_HUMOR_AGENT": {
        "token_id": "TEST_HUMOR_AGENT",
        "category": "humor_engine",
        "class": "properties",
        "content": 'humor: "agent_default"',
        "metadata": {"description": "Agent-level humor preset"},
    },
    "TEST_HUMOR_ACCOUNT": {
        "token_id": "TEST_HUMOR_ACCOUNT",
        "category": "humor_engine",
        "class": "properties",
        "content": 'humor: "account_override"',
        "metadata": {"description": "Account-level humor override"},
    },
    "TEST_HUMOR_USER": {
        "token_id": "TEST_HUMOR_USER",
        "category": "humor_engine",
        "class": "properties",
        "content": 'humor: "user_personal"',
        "metadata": {"description": "User-level humor override"},
    },
    "TEST_COGNITIVE_AGENT": {
        "token_id": "TEST_COGNITIVE_AGENT",
        "category": "cognitive_process",
        "class": "cognitive_process",
        "content": 'mode: "agent_standard"',
        "metadata": {"description": "Agent cognitive process"},
    },
}

_BLUEPRINT = {
    "blueprint_id": "test_integ_blueprint_v1",
    "outer_class": "TestBot extends Agent",
    "class_order": ["properties", "cognitive_process"],
}

# Agent profile — base tokens (AGENT level)
_AGENT_PROFILE = {
    "blueprint_id": "test_integ_blueprint_v1",
    "agent_id": "test_integ_agent",
    "tokens": {
        "TEST_HUMOR_AGENT": {"order": 10},
        "TEST_COGNITIVE_AGENT": {"order": 20},
    },
}

# Account override — replaces humor token
_ACCOUNT_OVERRIDE = {
    "owner_type": "ACCOUNT",
    "owner_id": TEST_ACCOUNT_ID,
    "tokens": {
        "TEST_HUMOR_ACCOUNT": {"order": 10},
    },
}

# User override — replaces humor token
_USER_OVERRIDE = {
    "owner_type": "USER",
    "owner_id": TEST_USER_ID,
    "tokens": {
        "TEST_HUMOR_USER": {"order": 10},
    },
}


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(scope="function")
async def real_firestore_db():
    """Real Firestore client connected to test environment."""
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
        pytest.skip("GOOGLE_CLOUD_PROJECT not set — cannot run Firestore integration test")

    db_name = os.getenv("FIRESTORE_DATABASE", "us-production")
    db = firestore.AsyncClient(project=project_id, database=db_name)
    yield db

    if original_app_env is None:
        os.environ.pop("APP_ENV", None)
    else:
        os.environ["APP_ENV"] = original_app_env


@pytest.fixture(scope="function")
async def seed_firestore(real_firestore_db):
    """Upload test tokens, blueprint, and agent profile. Clean up after test."""
    db = real_firestore_db

    # Upload tokens
    for token_id, token_data in _TOKENS.items():
        await db.collection(_COL_TOKENS).document(token_id).set(token_data)

    # Upload blueprint
    await db.collection(_COL_BLUEPRINTS).document(_BLUEPRINT["blueprint_id"]).set(_BLUEPRINT)

    # Upload agent profile (always present)
    await db.collection(_COL_PROFILES).document("test_integ_agent").set(_AGENT_PROFILE)

    yield db

    # Cleanup: delete all test documents
    for token_id in _TOKENS:
        await db.collection(_COL_TOKENS).document(token_id).delete()
    await db.collection(_COL_BLUEPRINTS).document(_BLUEPRINT["blueprint_id"]).delete()
    await db.collection(_COL_PROFILES).document("test_integ_agent").delete()
    await db.collection(_COL_OVERRIDES).document(f"ACCOUNT_{TEST_ACCOUNT_ID}").delete()
    await db.collection(_COL_OVERRIDES).document(f"USER_{TEST_USER_ID}").delete()


def _build_service(db) -> PromptAssemblyService:
    """Build PromptAssemblyService with real Firestore adapters pointing to test collections."""
    return PromptAssemblyService(
        token_repo=FirestoreTokenRepository(
            db=db,
            system_collection=_COL_TOKENS,
            user_collection=_COL_TOKENS,  # same collection for tests
            security_port=_PassthroughSecurityPort(),
        ),
        blueprint_repo=FirestoreBlueprintRepository(db=db, collection_name=_COL_BLUEPRINTS),
        profile_repo=FirestoreAgentProfileRepository(
            db=db,
            profiles_collection=_COL_PROFILES,
            overrides_collection=_COL_OVERRIDES,
        ),
        security_port=_PassthroughSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=BiographicalFactsFormatter(),
        cache_ttl=0,  # disable cache for tests
    )


# =============================================================================
# Test Scenario 1: AGENT + ACCOUNT → ACCOUNT wins
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_agent_plus_account_override(seed_firestore):
    """
    AGENT + ACCOUNT override → ACCOUNT wins for matching class+category.

    Setup: agent profile (humor=agent_default, cognitive=agent_standard)
           + ACCOUNT override (humor=account_override)
    Expect: humor → account_override, cognitive → agent_standard (untouched)
    """
    db = seed_firestore

    # Upload ACCOUNT override
    await db.collection(_COL_OVERRIDES).document(f"ACCOUNT_{TEST_ACCOUNT_ID}").set(
        _ACCOUNT_OVERRIDE
    )

    service = _build_service(db)
    assembled = await service.assemble(
        agent_type="test_integ_agent",
        user_id=None,
        account_id=TEST_ACCOUNT_ID,
    )

    # ACCOUNT humor override wins
    assert 'humor: "account_override"' in assembled, "ACCOUNT humor should be present"
    # Agent humor is replaced
    assert 'humor: "agent_default"' not in assembled, "AGENT humor should be overridden"
    # Cognitive process untouched (no override for it)
    assert 'mode: "agent_standard"' in assembled, "AGENT cognitive should remain"
    # Groovy structure present
    assert "class TestBot extends Agent {" in assembled


# =============================================================================
# Test Scenario 2: AGENT + USER → USER wins
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_agent_plus_user_override(seed_firestore):
    """
    AGENT + USER override → USER wins for matching class+category.

    Setup: agent profile (humor=agent_default, cognitive=agent_standard)
           + USER override (humor=user_personal)
    Expect: humor → user_personal, cognitive → agent_standard (untouched)
    """
    db = seed_firestore

    # Upload USER override (no account override)
    await db.collection(_COL_OVERRIDES).document(f"USER_{TEST_USER_ID}").set(
        _USER_OVERRIDE
    )

    service = _build_service(db)
    assembled = await service.assemble(
        agent_type="test_integ_agent",
        user_id=TEST_USER_ID,
        account_id=None,
    )

    # USER humor override wins
    assert 'humor: "user_personal"' in assembled, "USER humor should be present"
    # Agent humor is replaced
    assert 'humor: "agent_default"' not in assembled, "AGENT humor should be overridden"
    # Cognitive process untouched
    assert 'mode: "agent_standard"' in assembled, "AGENT cognitive should remain"


# =============================================================================
# Test Scenario 3: ALL levels → USER wins (highest priority)
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_all_levels_user_wins(seed_firestore):
    """
    AGENT + ACCOUNT + USER → USER wins (highest priority).

    Setup: agent profile (humor=agent_default, cognitive=agent_standard)
           + ACCOUNT override (humor=account_override)
           + USER override (humor=user_personal)
    Expect: humor → user_personal (USER beats ACCOUNT beats AGENT)
    """
    db = seed_firestore

    # Upload both ACCOUNT and USER overrides
    await db.collection(_COL_OVERRIDES).document(f"ACCOUNT_{TEST_ACCOUNT_ID}").set(
        _ACCOUNT_OVERRIDE
    )
    await db.collection(_COL_OVERRIDES).document(f"USER_{TEST_USER_ID}").set(
        _USER_OVERRIDE
    )

    service = _build_service(db)
    assembled = await service.assemble(
        agent_type="test_integ_agent",
        user_id=TEST_USER_ID,
        account_id=TEST_ACCOUNT_ID,
    )

    # USER override wins over both ACCOUNT and AGENT
    assert 'humor: "user_personal"' in assembled, "USER humor should win"
    assert 'humor: "account_override"' not in assembled, "ACCOUNT should be overridden by USER"
    assert 'humor: "agent_default"' not in assembled, "AGENT should be overridden by USER"
    # Cognitive process untouched (no override at any level)
    assert 'mode: "agent_standard"' in assembled, "AGENT cognitive should remain"
