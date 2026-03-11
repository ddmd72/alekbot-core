"""
Unit tests for ConfigurationService (OAuth Multi-Tenant Session 6).

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import pytest

from src.services.configuration_service import ConfigurationService
from src.domain.user import UserProfile, UserBotConfig
from src.domain.billing import BillingAccount, AccountTier


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture
def config_service():
    """Create ConfigurationService instance."""
    return ConfigurationService()


@pytest.fixture
def test_user_default():
    """User with default config (no customizations)."""
    return UserProfile(
        user_id="user-1",
        email="user1@example.com",
        display_name="User 1",
        account_id="account-1",
    )


@pytest.fixture
def test_user_custom():
    """User with custom config overrides."""
    return UserProfile(
        user_id="user-2",
        email="user2@example.com",
        display_name="User 2",
        account_id="account-1",
        config=UserBotConfig(
            temperature=0.9,
            agent_tiers={"quick": "balanced"},
        ),
    )


@pytest.fixture
def test_account_no_defaults():
    """Account with no defaults set."""
    return BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"user-1": "owner"},
    )


@pytest.fixture
def test_account_with_defaults():
    """Account with defaults configured."""
    return BillingAccount(
        account_id="account-1",
        tier=AccountTier.PRO,
        iam_policy={"user-1": "owner", "user-2": "member"},
        account_defaults=UserBotConfig(
            temperature=0.7,
            default_tier="eco",
            agent_tiers={"router": "eco", "planning": "balanced"},
        ),
    )


# ============================================================================
# get_effective_config() Tests
# ============================================================================
def test_get_effective_config_no_account_defaults(
    config_service, test_user_custom, test_account_no_defaults
):
    """Test Case 1: No account defaults → use user config as-is."""
    effective = config_service.get_effective_config(
        test_user_custom, test_account_no_defaults
    )

    # Should return user config unchanged
    assert effective.temperature == 0.9
    assert effective.agent_tiers == {"quick": "balanced"}


def test_get_effective_config_user_has_no_overrides(
    config_service, test_user_default, test_account_with_defaults
):
    """Test Case 2: User has default config → use account defaults."""
    effective = config_service.get_effective_config(
        test_user_default, test_account_with_defaults
    )

    # Should return account defaults
    assert effective.temperature == 0.7
    assert effective.default_tier == "eco"
    assert effective.agent_tiers == {"router": "eco", "planning": "balanced"}


def test_get_effective_config_merge(
    config_service, test_user_custom, test_account_with_defaults
):
    """Test Case 3: User has overrides → merge account defaults + user overrides."""
    effective = config_service.get_effective_config(
        test_user_custom, test_account_with_defaults
    )

    # User temperature override should win
    assert effective.temperature == 0.9

    # Account default_tier should be used (user didn't override)
    assert effective.default_tier == "eco"

    # Dict merge: account tiers + user tiers
    assert effective.agent_tiers == {
        "router": "eco",  # from account
        "planning": "balanced",  # from account
        "quick": "balanced",  # from user
    }


def test_get_effective_config_user_overrides_scalar_field(config_service):
    """Test scalar field override (temperature)."""
    user = UserProfile(
        user_id="user-3",
        email="user3@example.com",
        account_id="account-1",
        config=UserBotConfig(temperature=0.95),
    )

    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"user-3": "member"},
        account_defaults=UserBotConfig(temperature=0.7),
    )

    effective = config_service.get_effective_config(user, account)

    # User override should win
    assert effective.temperature == 0.95


def test_get_effective_config_dict_deep_merge(config_service):
    """Test dict field deep merge (agent_tiers)."""
    user = UserProfile(
        user_id="user-4",
        email="user4@example.com",
        account_id="account-1",
        config=UserBotConfig(
            agent_tiers={"quick": "eco", "specialist": "eco"}
        ),
    )

    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"user-4": "member"},
        account_defaults=UserBotConfig(
            agent_tiers={"router": "balanced", "planning": "eco"}
        ),
    )

    effective = config_service.get_effective_config(user, account)

    # Should have keys from both account and user
    assert effective.agent_tiers == {
        "router": "balanced",  # from account
        "planning": "eco",  # from account
        "quick": "eco",  # from user
        "specialist": "eco",  # from user
    }


def test_get_effective_config_empty_account_defaults(config_service):
    """Test with account_defaults = UserBotConfig() (all defaults)."""
    user = UserProfile(
        user_id="user-5",
        email="user5@example.com",
        account_id="account-1",
        config=UserBotConfig(temperature=0.8),
    )

    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"user-5": "owner"},
        account_defaults=UserBotConfig(),  # Empty defaults
    )

    effective = config_service.get_effective_config(user, account)

    # User override should still apply
    assert effective.temperature == 0.8


# ============================================================================
# _is_default_config() Tests
# ============================================================================
def test_is_default_config_true(config_service):
    """Test detection of default config (no customizations)."""
    default_config = UserBotConfig()

    assert config_service._is_default_config(default_config) is True


def test_is_default_config_false_scalar_override(config_service):
    """Test detection of customized config (scalar field changed)."""
    custom_config = UserBotConfig(temperature=0.9)

    assert config_service._is_default_config(custom_config) is False


def test_is_default_config_false_dict_override(config_service):
    """Test detection of customized config (dict field changed)."""
    custom_config = UserBotConfig(agent_tiers={"router": "eco"})

    assert config_service._is_default_config(custom_config) is False


def test_is_default_config_false_multiple_overrides(config_service):
    """Test detection of customized config (multiple fields changed)."""
    custom_config = UserBotConfig(
        temperature=0.85,
        default_tier="balanced",
        agent_tiers={"quick": "eco"},
    )

    assert config_service._is_default_config(custom_config) is False


# ============================================================================
# _merge_configs() Tests
# ============================================================================
def test_merge_configs_scalar_override(config_service):
    """Test merging with scalar field override."""
    base = UserBotConfig(temperature=0.7, default_tier="eco")
    overrides = UserBotConfig(temperature=0.9)

    merged = config_service._merge_configs(base, overrides)

    # Override should win for temperature
    assert merged.temperature == 0.9
    # Base should be used for default_tier
    assert merged.default_tier == "eco"


def test_merge_configs_dict_deep_merge(config_service):
    """Test merging with dict deep merge."""
    base = UserBotConfig(
        agent_tiers={"router": "eco", "planning": "balanced"}
    )
    overrides = UserBotConfig(
        agent_tiers={"quick": "eco", "specialist": "eco"}
    )

    merged = config_service._merge_configs(base, overrides)

    # Should have keys from both base and overrides
    assert merged.agent_tiers == {
        "router": "eco",
        "planning": "balanced",
        "quick": "eco",
        "specialist": "eco",
    }


def test_merge_configs_override_wins_in_dict_conflict(config_service):
    """Test dict merge where override wins for conflicting keys."""
    base = UserBotConfig(agent_tiers={"router": "eco"})
    overrides = UserBotConfig(agent_tiers={"router": "eco"})

    merged = config_service._merge_configs(base, overrides)

    # Override should win
    assert merged.agent_tiers == {"router": "eco"}


def test_merge_configs_all_fields_default(config_service):
    """Test merging when override config has all defaults."""
    base = UserBotConfig(temperature=0.7, default_tier="eco")
    overrides = UserBotConfig()  # All defaults

    merged = config_service._merge_configs(base, overrides)

    # Should use base values
    assert merged.temperature == 0.7
    assert merged.default_tier == "eco"


def test_merge_configs_empty_dicts(config_service):
    """Test merging with empty dict fields."""
    base = UserBotConfig(agent_tiers={})
    overrides = UserBotConfig(agent_tiers={"quick": "eco"})

    merged = config_service._merge_configs(base, overrides)

    # Should have override dict
    assert merged.agent_tiers == {"quick": "eco"}


# ============================================================================
# has_user_overrides() Tests
# ============================================================================
def test_has_user_overrides_false(config_service, test_user_default):
    """Test user with no overrides."""
    assert config_service.has_user_overrides(test_user_default) is False


def test_has_user_overrides_true(config_service, test_user_custom):
    """Test user with custom overrides."""
    assert config_service.has_user_overrides(test_user_custom) is True


def test_has_user_overrides_single_field(config_service):
    """Test user with single field override."""
    user = UserProfile(
        user_id="user-6",
        email="user6@example.com",
        account_id="account-1",
        config=UserBotConfig(temperature=0.8),
    )

    assert config_service.has_user_overrides(user) is True


# ============================================================================
# get_override_summary() Tests
# ============================================================================
def test_get_override_summary_no_account_defaults(
    config_service, test_user_custom, test_account_no_defaults
):
    """Test override summary when account has no defaults."""
    summary = config_service.get_override_summary(
        test_user_custom, test_account_no_defaults
    )

    # Should return empty dict (no comparison possible)
    assert summary == {}


def test_get_override_summary_no_user_overrides(
    config_service, test_user_default, test_account_with_defaults
):
    """Test override summary when user has no overrides."""
    summary = config_service.get_override_summary(
        test_user_default, test_account_with_defaults
    )

    # Should return empty dict (user uses defaults)
    assert summary == {}


def test_get_override_summary_with_overrides(
    config_service, test_user_custom, test_account_with_defaults
):
    """Test override summary when user has custom overrides."""
    summary = config_service.get_override_summary(
        test_user_custom, test_account_with_defaults
    )

    # Should show overridden fields
    assert "temperature" in summary
    assert summary["temperature"]["account_default"] == 0.7
    assert summary["temperature"]["user_override"] == 0.9

    assert "agent_tiers" in summary
    assert summary["agent_tiers"]["account_default"] == {
        "router": "eco",
        "planning": "balanced",
    }
    assert summary["agent_tiers"]["user_override"] == {"quick": "balanced"}


def test_get_override_summary_user_matches_account(config_service):
    """Test override summary when user value matches account default."""
    user = UserProfile(
        user_id="user-7",
        email="user7@example.com",
        account_id="account-1",
        config=UserBotConfig(temperature=0.7),  # Same as account
    )

    account = BillingAccount(
        account_id="account-1",
        tier=AccountTier.FREE,
        iam_policy={"user-7": "member"},
        account_defaults=UserBotConfig(temperature=0.7),
    )

    summary = config_service.get_override_summary(user, account)

    # Should be empty (user value matches account default)
    assert summary == {}


# ============================================================================
# reset_user_config() Tests
# ============================================================================
def test_reset_user_config(config_service, test_user_custom):
    """Test resetting user config to defaults."""
    # Verify user has custom config
    assert test_user_custom.config.temperature == 0.9

    # Reset config
    updated_user = config_service.reset_user_config(test_user_custom)

    # Config should be default
    assert updated_user.config.temperature == UserBotConfig().temperature
    assert updated_user.config.agent_tiers == UserBotConfig().agent_tiers


def test_reset_user_config_already_default(config_service, test_user_default):
    """Test resetting user config that's already default."""
    updated_user = config_service.reset_user_config(test_user_default)

    # Should still be default
    default_config = UserBotConfig()
    assert updated_user.config.temperature == default_config.temperature


# ============================================================================
# apply_account_defaults() Tests
# ============================================================================
def test_apply_account_defaults(config_service, test_account_no_defaults):
    """Test updating account defaults."""
    new_defaults = UserBotConfig(
        temperature=0.8,
        default_tier="balanced",
        agent_tiers={"router": "eco"},
    )

    # Apply new defaults
    updated_account = config_service.apply_account_defaults(
        test_account_no_defaults, new_defaults
    )

    # Account should have new defaults
    assert updated_account.account_defaults is not None
    assert updated_account.account_defaults.temperature == 0.8
    assert updated_account.account_defaults.default_tier == "balanced"
    assert updated_account.account_defaults.agent_tiers == {"router": "eco"}


def test_apply_account_defaults_replace_existing(
    config_service, test_account_with_defaults
):
    """Test replacing existing account defaults."""
    # Verify existing defaults
    assert test_account_with_defaults.account_defaults.temperature == 0.7

    new_defaults = UserBotConfig(temperature=0.95, default_tier="eco")

    # Apply new defaults
    updated_account = config_service.apply_account_defaults(
        test_account_with_defaults, new_defaults
    )

    # Account should have new defaults
    assert updated_account.account_defaults.temperature == 0.95
    assert updated_account.account_defaults.default_tier == "eco"


def test_apply_account_defaults_empty(config_service, test_account_with_defaults):
    """Test setting account defaults to empty config."""
    empty_defaults = UserBotConfig()

    updated_account = config_service.apply_account_defaults(
        test_account_with_defaults, empty_defaults
    )

    # Account should have empty defaults
    assert updated_account.account_defaults is not None
    default_config = UserBotConfig()
    assert updated_account.account_defaults.temperature == default_config.temperature
