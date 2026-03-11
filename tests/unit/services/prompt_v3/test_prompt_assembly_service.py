"""
Unit tests for PromptAssemblyService — v4 class-collection assembly model.

Tests:
- Basic assembly: tokens grouped by class, sorted by order, wrapped in sections
- Override semantics: account then user overrides by class+category match
- non_overridable flag blocks replacement
- Empty profile produces minimal output
- Runtime context injection (unchanged from v3)
- Security validation called for untrusted data
"""

import pytest
from unittest.mock import AsyncMock, Mock

from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.domain.prompt_v3.token import Token, TokenId, TokenCategory, TokenClass
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.agent_profile import AgentProfile
from src.ports.security_port import SecurityPort, ValidationResult, RiskLevel, TrustZone


class MockSecurityPort(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(
            sanitized_text=text,
            risk_level=RiskLevel.SAFE,
            risk_score=0.0,
            patterns_detected=[],
            action_taken="passed",
            metadata={"adapter": "mock"}
        )


def _make_token(token_id: str, class_: str, category: str, content: str) -> Token:
    return Token(
        id=TokenId(token_id),
        category=TokenCategory(category),
        class_=TokenClass(class_),
        content=content,
        metadata={}
    )


def _make_blueprint(
    class_order=None,
    outer_class="Alek extends Agent"
) -> Blueprint:
    return Blueprint(
        id="universal_agent_v1",
        outer_class=outer_class,
        class_order=class_order or ["properties", "cognitive_process"],
    )


# Default token documents shared across tests
_DEFAULT_TOKEN_DOCS = {
    TokenId("HUMOR_PRESET_RANEVSKAYA"): _make_token(
        "HUMOR_PRESET_RANEVSKAYA", "properties", "humor_engine", 'style: "ranevskaya"'
    ),
    TokenId("COGNITIVE_PROCESS_QUICK"): _make_token(
        "COGNITIVE_PROCESS_QUICK", "cognitive_process", "cognitive_process",
        'steps: ["think fast"]'
    ),
    TokenId("HUMOR_PRESET_OFF"): _make_token(
        "HUMOR_PRESET_OFF", "properties", "humor_engine", 'style: "professional"'
    ),
}


@pytest.fixture
def mock_repos():
    """Mock repositories with two agent tokens (properties + cognitive_process)."""
    token_repo = AsyncMock()
    blueprint_repo = AsyncMock()
    profile_repo = AsyncMock()
    bio_formatter = Mock()
    bio_formatter.format.return_value = ""

    blueprint_repo.get = AsyncMock(return_value=_make_blueprint())
    profile_repo.get_agent_profile = AsyncMock(return_value=AgentProfile(
        blueprint_id="universal_agent_v1",
        tokens={
            "HUMOR_PRESET_RANEVSKAYA": ProfileToken(
                token_id="HUMOR_PRESET_RANEVSKAYA", order=40
            ),
            "COGNITIVE_PROCESS_QUICK": ProfileToken(
                token_id="COGNITIVE_PROCESS_QUICK", order=10, non_overridable=True
            ),
        }
    ))
    profile_repo.get_override_tokens = AsyncMock(return_value={})
    token_repo.get = AsyncMock(side_effect=lambda tid: _DEFAULT_TOKEN_DOCS.get(tid))

    return token_repo, blueprint_repo, profile_repo, bio_formatter


@pytest.fixture
def service(mock_repos):
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos
    return PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=MockSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )


# =============================================================================
# Basic assembly
# =============================================================================

@pytest.mark.asyncio
async def test_assembly_produces_groovy_class_structure(service):
    """Assembled prompt wraps tokens in a Groovy class with section wrappers."""
    prompt = await service.assemble(
        agent_type="quick",
        user_id=None,
        account_id=None,
    )

    assert "class Alek extends Agent {" in prompt
    assert "properties {" in prompt
    assert "cognitive_process {" in prompt


@pytest.mark.asyncio
async def test_assembly_includes_token_content(service):
    """All active token content is present in the assembled prompt."""
    prompt = await service.assemble(
        agent_type="quick",
        user_id=None,
        account_id=None,
    )

    assert 'style: "ranevskaya"' in prompt
    assert 'steps: ["think fast"]' in prompt


@pytest.mark.asyncio
async def test_assembly_skips_empty_sections(mock_repos):
    """Sections with no tokens are omitted from the output."""
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos

    # Blueprint has 3 classes; agent only has tokens for 2
    blueprint_repo.get = AsyncMock(return_value=_make_blueprint(
        class_order=["properties", "cognitive_process", "policies"]
    ))

    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=MockSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )
    prompt = await service.assemble(agent_type="quick", user_id=None, account_id=None)

    assert "policies" not in prompt


@pytest.mark.asyncio
async def test_assembly_token_order_within_section(mock_repos):
    """Multiple tokens in same section are sorted ascending by ProfileToken.order."""
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos

    token_a = _make_token("TOKEN_A", "properties", "category_a", "content_A")
    token_b = _make_token("TOKEN_B", "properties", "category_b", "content_B")

    profile_repo.get_agent_profile = AsyncMock(return_value=AgentProfile(
        blueprint_id="universal_agent_v1",
        tokens={
            "TOKEN_A": ProfileToken(token_id="TOKEN_A", order=20),
            "TOKEN_B": ProfileToken(token_id="TOKEN_B", order=10),  # lower → rendered first
        }
    ))
    blueprint_repo.get = AsyncMock(return_value=_make_blueprint(class_order=["properties"]))
    token_repo.get = AsyncMock(side_effect=lambda tid: {
        TokenId("TOKEN_A"): token_a,
        TokenId("TOKEN_B"): token_b,
    }.get(tid))

    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=MockSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )
    prompt = await service.assemble(agent_type="quick", user_id=None, account_id=None)

    assert "content_B" in prompt
    assert "content_A" in prompt
    # content_B (order=10) must appear before content_A (order=20)
    assert prompt.index("content_B") < prompt.index("content_A")


# =============================================================================
# Override semantics
# =============================================================================

@pytest.mark.asyncio
async def test_account_override_replaces_token_by_class_category(mock_repos):
    """Account override with matching class+category replaces the agent token."""
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos

    profile_repo.get_override_tokens = AsyncMock(side_effect=lambda owner_type, owner_id: (
        {"HUMOR_PRESET_OFF": ProfileToken(token_id="HUMOR_PRESET_OFF", order=40)}
        if owner_type == OwnerType.ACCOUNT else {}
    ))

    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=MockSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )
    prompt = await service.assemble(
        agent_type="quick", user_id=None, account_id="account_123"
    )

    assert 'style: "professional"' in prompt      # override applied
    assert 'style: "ranevskaya"' not in prompt    # original replaced


@pytest.mark.asyncio
async def test_user_override_takes_priority_over_account(mock_repos):
    """User override wins over account override for the same class+category."""
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos

    humor_custom = _make_token(
        "HUMOR_CUSTOM", "properties", "humor_engine", 'style: "custom_user"'
    )
    all_docs = {**_DEFAULT_TOKEN_DOCS, TokenId("HUMOR_CUSTOM"): humor_custom}
    token_repo.get = AsyncMock(side_effect=lambda tid: all_docs.get(tid))

    profile_repo.get_override_tokens = AsyncMock(side_effect=lambda owner_type, owner_id: {
        OwnerType.ACCOUNT: {"HUMOR_PRESET_OFF": ProfileToken("HUMOR_PRESET_OFF", order=40)},
        OwnerType.USER: {"HUMOR_CUSTOM": ProfileToken("HUMOR_CUSTOM", order=40)},
    }.get(owner_type, {}))

    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=MockSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )
    prompt = await service.assemble(
        agent_type="quick", user_id="user_123", account_id="account_123"
    )

    assert 'style: "custom_user"' in prompt       # user wins
    assert 'style: "professional"' not in prompt  # account overridden by user
    assert 'style: "ranevskaya"' not in prompt    # original gone


@pytest.mark.asyncio
async def test_override_ignored_when_no_matching_class_category(mock_repos):
    """Override for a class+category not in agent profile is silently ignored."""
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos

    # policies category has no matching token in agent profile
    policies_token = _make_token(
        "POLICIES_STRICT", "policies", "safety_policy", "no_dangerous_content: true"
    )
    all_docs = {**_DEFAULT_TOKEN_DOCS, TokenId("POLICIES_STRICT"): policies_token}
    token_repo.get = AsyncMock(side_effect=lambda tid: all_docs.get(tid))

    profile_repo.get_override_tokens = AsyncMock(side_effect=lambda owner_type, owner_id: (
        {"POLICIES_STRICT": ProfileToken("POLICIES_STRICT", order=10)}
        if owner_type == OwnerType.ACCOUNT else {}
    ))

    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=MockSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )
    prompt = await service.assemble(
        agent_type="quick", user_id=None, account_id="account_123"
    )

    assert "no_dangerous_content" not in prompt   # override ignored
    assert 'style: "ranevskaya"' in prompt        # original preserved


@pytest.mark.asyncio
async def test_non_overridable_blocks_replacement(mock_repos):
    """Token with non_overridable=True cannot be replaced by user override."""
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos

    smart_cp = _make_token(
        "COGNITIVE_PROCESS_SMART", "cognitive_process", "cognitive_process",
        'steps: ["think deeply"]'
    )
    all_docs = {**_DEFAULT_TOKEN_DOCS, TokenId("COGNITIVE_PROCESS_SMART"): smart_cp}
    token_repo.get = AsyncMock(side_effect=lambda tid: all_docs.get(tid))

    profile_repo.get_override_tokens = AsyncMock(side_effect=lambda owner_type, owner_id: (
        {"COGNITIVE_PROCESS_SMART": ProfileToken("COGNITIVE_PROCESS_SMART", order=10)}
        if owner_type == OwnerType.USER else {}
    ))

    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=MockSecurityPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )
    prompt = await service.assemble(
        agent_type="quick", user_id="user_123", account_id=None
    )

    assert 'steps: ["think fast"]' in prompt       # original preserved
    assert 'steps: ["think deeply"]' not in prompt  # blocked by non_overridable


# =============================================================================
# Runtime context injection
# =============================================================================

@pytest.mark.asyncio
async def test_biographical_facts_injected_before_cache_boundary(service):
    """Biographical facts appear before the cache boundary marker."""
    from src.ports.llm_port import PROMPT_CACHE_BOUNDARY
    service.bio_formatter.format.return_value = "- Born in Kyiv\n- Software engineer"

    prompt = await service.assemble(
        agent_type="quick",
        user_id="u",
        account_id=None,
        biographical_facts=[{"text": "Born in Kyiv", "tags": []}],
    )

    boundary_pos = prompt.index(PROMPT_CACHE_BOUNDARY)
    static_part = prompt[:boundary_pos]
    assert "Born in Kyiv" in static_part


@pytest.mark.asyncio
async def test_security_validation_called_for_runtime_data(mock_repos):
    """SecurityPort.validate() is called for biographical and conversation data."""
    token_repo, blueprint_repo, profile_repo, bio_formatter = mock_repos
    bio_formatter.format.return_value = "Some fact"

    validate_calls = []

    class TrackingPort(SecurityPort):
        async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
            validate_calls.append(context)
            return ValidationResult(
                sanitized_text=text,
                risk_level=RiskLevel.SAFE,
                risk_score=0.0,
                patterns_detected=[],
                action_taken="passed",
                metadata={}
            )

    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=TrackingPort(),
        formatter=ContextFormatter(),
        bio_formatter=bio_formatter,
    )

    await service.assemble(
        agent_type="quick",
        user_id="user_123",
        account_id=None,
        biographical_facts=[{"text": "fact", "tags": []}],
        conversation_history=[{"role": "user", "content": "hi"}],
    )

    assert any("biographical" in c for c in validate_calls)
    assert any("conversation" in c for c in validate_calls)
