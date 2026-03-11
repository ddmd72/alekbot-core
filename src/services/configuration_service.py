"""
Configuration Service (OAuth Multi-Tenant Session 6).

Implements configuration inheritance for multi-tenant architecture.
Account defaults + User overrides = Effective configuration.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
from typing import Optional, Dict, Any, List

from ..domain.user import UserProfile, UserBotConfig
from ..domain.billing import BillingAccount
from ..utils.logger import logger


class ConfigurationService:
    """
    Configuration inheritance service.

    Implements "Configuration Inheritance" pattern for multi-tenant architecture:
    - 99% of users use account defaults (BillingAccount.account_defaults)
    - 1% of power users override specific settings (UserProfile.config)
    - Merge logic: User config overrides account defaults field-by-field

    Example:
        Account defaults: {temperature: 0.7, default_tier: "eco"}
        User overrides:   {temperature: 0.9}
        Effective config: {temperature: 0.9, default_tier: "eco"}

    Use Cases:
    - Family account: Parent sets defaults, children use them
    - Team account: Admin sets team defaults, members customize
    - Power users: Override specific settings without affecting others
    """

    def get_effective_config(
        self,
        user: UserProfile,
        account: BillingAccount,
    ) -> UserBotConfig:
        """
        Get effective configuration for user by merging account defaults with user overrides.

        Logic:
        1. If account has no defaults → use user config as-is
        2. If user config is default (all defaults) → use account defaults
        3. Otherwise → merge: account defaults + user overrides

        Args:
            user: User profile with config overrides
            account: Billing account with account_defaults

        Returns:
            Effective UserBotConfig (merged configuration)

        Example:
            account.account_defaults = UserBotConfig(temperature=0.7, default_tier="eco")
            user.config = UserBotConfig(temperature=0.9)  # only temperature overridden
            result = UserBotConfig(temperature=0.9, default_tier="eco")  # merged
        """
        # Case 1: No account defaults → use user config as-is
        if not account.account_defaults:
            logger.debug(
                f"⚙️ Using user config (no account defaults) - user: {user.user_id}"
            )
            return user.config

        # Case 2: User config is default → use account defaults
        if self._is_default_config(user.config):
            logger.debug(
                f"⚙️ Using account defaults (user has no overrides) - user: {user.user_id}"
            )
            return account.account_defaults

        # Case 3: Merge account defaults + user overrides
        merged_config = self._merge_configs(
            base=account.account_defaults,
            overrides=user.config,
        )

        logger.debug(
            f"⚙️ Using merged config (account defaults + user overrides) - user: {user.user_id}"
        )

        return merged_config

    def _is_default_config(self, config: UserBotConfig) -> bool:
        """
        Check if user config is all defaults (no customizations).

        Used to detect if user has made any customizations.
        If config equals default UserBotConfig(), use account defaults instead.

        Args:
            config: User's bot configuration

        Returns:
            True if config is all defaults, False if user customized anything
        """
        default_config = UserBotConfig()

        # Compare field by field
        # Exclude computed fields and use model_dump for comparison
        user_dict = config.model_dump(exclude_none=False)
        default_dict = default_config.model_dump(exclude_none=False)

        return user_dict == default_dict

    def _merge_configs(
        self,
        base: UserBotConfig,
        overrides: UserBotConfig,
    ) -> UserBotConfig:
        """
        Merge two UserBotConfig instances: base + overrides.

        Merge logic (field-by-field):
        - Scalar fields (temperature, default_tier): Use override if different from default
        - Dict fields (agent_tiers, model_overrides): Deep merge (base + override keys)
        - List fields (tools_enabled): Use override if not default
        - Nested objects (prompt_preferences): Deep merge

        Args:
            base: Account defaults (base configuration)
            overrides: User config (override configuration)

        Returns:
            Merged UserBotConfig

        Example:
            base = UserBotConfig(temperature=0.7, agent_tiers={"router": "eco"})
            overrides = UserBotConfig(temperature=0.9, agent_tiers={"quick": "balanced"})
            result = UserBotConfig(temperature=0.9, agent_tiers={"router": "eco", "quick": "balanced"})
        """
        # Get default config for comparison
        default_config = UserBotConfig()
        default_dict = default_config.model_dump(exclude_none=False)

        # Convert to dicts for merging
        base_dict = base.model_dump(exclude_none=False)
        override_dict = overrides.model_dump(exclude_none=False)

        # Merge logic
        merged_dict: Dict[str, Any] = {}

        for field_name, default_value in default_dict.items():
            base_value = base_dict.get(field_name, default_value)
            override_value = override_dict.get(field_name, default_value)

            # If override is different from default, use override
            if override_value != default_value:
                # Special handling for dicts: deep merge
                if isinstance(override_value, dict) and isinstance(base_value, dict):
                    merged_dict[field_name] = {**base_value, **override_value}
                else:
                    merged_dict[field_name] = override_value
            else:
                # Use base value
                merged_dict[field_name] = base_value

        # Create merged config from dict
        merged_config = UserBotConfig(**merged_dict)

        return merged_config

    def has_user_overrides(self, user: UserProfile) -> bool:
        """
        Check if user has any configuration overrides.

        Useful for UI to show "You're using custom settings" indicator.

        Args:
            user: User profile

        Returns:
            True if user has customizations, False if using all defaults
        """
        return not self._is_default_config(user.config)

    def get_override_summary(self, user: UserProfile, account: BillingAccount) -> Dict[str, Any]:
        """
        Get summary of user's configuration overrides vs account defaults.

        Useful for UI to show "What's different from account defaults?".

        Args:
            user: User profile
            account: Billing account

        Returns:
            Dict of field_name → (account_value, user_value) for overridden fields

        Example:
            {
                "temperature": (0.7, 0.9),
                "default_tier": ("eco", "balanced")
            }
        """
        if not account.account_defaults:
            return {}

        default_dict = UserBotConfig().model_dump(exclude_none=False)
        account_dict = account.account_defaults.model_dump(exclude_none=False)
        user_dict = user.config.model_dump(exclude_none=False)

        overrides = {}

        for field_name, default_value in default_dict.items():
            user_value = user_dict.get(field_name, default_value)
            account_value = account_dict.get(field_name, default_value)

            # Check if user overrode this field
            if user_value != default_value and user_value != account_value:
                overrides[field_name] = {
                    "account_default": account_value,
                    "user_override": user_value,
                }

        return overrides

    def reset_user_config(self, user: UserProfile) -> UserProfile:
        """
        Reset user configuration to defaults (remove all overrides).

        Useful for "Reset to defaults" feature in UI.

        Args:
            user: User profile

        Returns:
            Updated user profile with default config
        """
        user.config = UserBotConfig()
        logger.info(f"🔄 Reset user config to defaults - user: {user.user_id}")
        return user

    def apply_account_defaults(self, account: BillingAccount, defaults: UserBotConfig) -> BillingAccount:
        """
        Update account defaults (applies to all members without overrides).

        Useful for account admin to set team-wide defaults.

        Args:
            account: Billing account
            defaults: New account defaults

        Returns:
            Updated billing account

        Note:
            This affects all account members who don't have user overrides.
        """
        account.account_defaults = defaults
        logger.info(
            f"⚙️ Updated account defaults - account: {account.account_id}, "
            f"affects {len(account.iam_policy)} members"
        )
        return account

    def get_semantic_search_limit(
        self,
        user_config: UserBotConfig,
        account_defaults: Optional[UserBotConfig] = None
    ) -> int:
        """
        Resolve semantic search limit with 3-level priority.

        Priority (highest to lowest):
        1. USER override (user_config.semantic_search_limit)
        2. ACCOUNT default (account_defaults.semantic_search_limit)
        3. SYSTEM default (SearchConfig.DEFAULT_SEMANTIC_SEARCH_LIMIT)

        Session: 2026-02-07 Multi-Vector Semantic Search
        Plan: docs/SESSION_2026_02_07_MULTI_VECTOR_SEMANTIC_SEARCH.md

        Args:
            user_config: User's bot configuration
            account_defaults: Account-level defaults (optional)

        Returns:
            Resolved semantic search limit (20-100)

        Example:
            # User override
            user_config.semantic_search_limit = 100  # → 100

            # Account default
            account_defaults.semantic_search_limit = 50  # → 50

            # System default
            no overrides  # → 30
        """
        from ..domain.settings import SearchConfig

        # Level 1: User override
        if user_config.semantic_search_limit is not None:
            logger.debug(
                f"🔍 Using USER semantic_search_limit={user_config.semantic_search_limit}"
            )
            return user_config.semantic_search_limit

        # Level 2: Account default
        if account_defaults and account_defaults.semantic_search_limit is not None:
            logger.debug(
                f"🔍 Using ACCOUNT semantic_search_limit={account_defaults.semantic_search_limit}"
            )
            return account_defaults.semantic_search_limit

        # Level 3: System default
        search_config = SearchConfig()
        logger.debug(
            f"🔍 Using SYSTEM semantic_search_limit={search_config.DEFAULT_SEMANTIC_SEARCH_LIMIT}"
        )
        return search_config.DEFAULT_SEMANTIC_SEARCH_LIMIT

    def get_biographical_cache_limit(
        self,
        user_config: UserBotConfig,
        account_defaults: Optional[UserBotConfig] = None
    ) -> int:
        """
        Resolve biographical cache limit with 3-level priority.

        Priority (highest to lowest):
        1. USER override (user_config.biographical_cache_limit)
        2. ACCOUNT default (account_defaults.biographical_cache_limit)
        3. SYSTEM default (SearchConfig.DEFAULT_BIOGRAPHICAL_CACHE_LIMIT)

        Session: 2026-02-07 Biographical Cache Optimization
        Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_OPTIMIZATION.md
        RFC: docs/10_rfcs/BIOGRAPHICAL_CACHE_MULTI_VECTOR_RFC.md

        Args:
            user_config: User's bot configuration
            account_defaults: Account-level defaults (optional)

        Returns:
            Resolved biographical cache limit (30-100)

        Example:
            # FREE tier: 30, FAMILY: 50, PRO: 70, ENTERPRISE: 100
            # User can override to any value
        """
        from ..domain.settings import SearchConfig

        # Level 1: User override
        if user_config.biographical_cache_limit is not None:
            logger.debug(
                f"📚 Using USER biographical_cache_limit={user_config.biographical_cache_limit}"
            )
            return user_config.biographical_cache_limit

        # Level 2: Account default
        if account_defaults and account_defaults.biographical_cache_limit is not None:
            logger.debug(
                f"📚 Using ACCOUNT biographical_cache_limit={account_defaults.biographical_cache_limit}"
            )
            return account_defaults.biographical_cache_limit

        # Level 3: System default
        search_config = SearchConfig()
        logger.debug(
            f"📚 Using SYSTEM biographical_cache_limit={search_config.DEFAULT_BIOGRAPHICAL_CACHE_LIMIT}"
        )
        return search_config.DEFAULT_BIOGRAPHICAL_CACHE_LIMIT

    def get_principles_cache_limit(
        self,
        user_config: UserBotConfig,
        account_defaults: Optional[UserBotConfig] = None
    ) -> int:
        """
        Resolve principles cache limit with 3-level priority.

        Priority (highest to lowest):
        1. USER override (user_config.principles_cache_limit)
        2. ACCOUNT default (account_defaults.principles_cache_limit)
        3. SYSTEM default (SearchConfig.DEFAULT_PRINCIPLES_CACHE_LIMIT)

        Session: 2026-02-07 Biographical Cache Optimization
        Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_OPTIMIZATION.md
        RFC: docs/10_rfcs/BIOGRAPHICAL_CACHE_MULTI_VECTOR_RFC.md

        Args:
            user_config: User's bot configuration
            account_defaults: Account-level defaults (optional)

        Returns:
            Resolved principles cache limit (10-25)

        Example:
            # FREE tier: 10, FAMILY: 15, PRO: 20, ENTERPRISE: 25
            # User can override to any value
        """
        from ..domain.settings import SearchConfig

        # Level 1: User override
        if user_config.principles_cache_limit is not None:
            logger.debug(
                f"⚖️ Using USER principles_cache_limit={user_config.principles_cache_limit}"
            )
            return user_config.principles_cache_limit

        # Level 2: Account default
        if account_defaults and account_defaults.principles_cache_limit is not None:
            logger.debug(
                f"⚖️ Using ACCOUNT principles_cache_limit={account_defaults.principles_cache_limit}"
            )
            return account_defaults.principles_cache_limit

        # Level 3: System default
        search_config = SearchConfig()
        logger.debug(
            f"⚖️ Using SYSTEM principles_cache_limit={search_config.DEFAULT_PRINCIPLES_CACHE_LIMIT}"
        )
        return search_config.DEFAULT_PRINCIPLES_CACHE_LIMIT

    def get_history_recent_full_turns(
        self,
        user_config: UserBotConfig,
        account_defaults: Optional[UserBotConfig] = None
    ) -> int:
        """
        Resolve history_recent_full_turns with 3-level priority.

        Priority (highest to lowest):
        1. USER override (user_config.history_recent_full_turns)
        2. ACCOUNT default (account_defaults.history_recent_full_turns)
        3. SYSTEM default (SearchConfig.DEFAULT_HISTORY_RECENT_FULL_TURNS = 5)

        Controls how many recent model turns use full_text instead of summary.
        Older turns beyond this window use the compressed text field.
        """
        from ..domain.settings import SearchConfig

        # Level 1: User override
        if user_config.history_recent_full_turns is not None:
            logger.debug(
                f"📜 Using USER history_recent_full_turns={user_config.history_recent_full_turns}"
            )
            return user_config.history_recent_full_turns

        # Level 2: Account default
        if account_defaults and account_defaults.history_recent_full_turns is not None:
            logger.debug(
                f"📜 Using ACCOUNT history_recent_full_turns={account_defaults.history_recent_full_turns}"
            )
            return account_defaults.history_recent_full_turns

        # Level 3: System default
        search_config = SearchConfig()
        logger.debug(
            f"📜 Using SYSTEM history_recent_full_turns={search_config.DEFAULT_HISTORY_RECENT_FULL_TURNS}"
        )
        return search_config.DEFAULT_HISTORY_RECENT_FULL_TURNS

    def get_bio_keywords_query1(
        self,
        user_config: UserBotConfig,
        account_defaults: Optional[UserBotConfig] = None
    ) -> List[str]:
        """
        Resolve biographical keywords query1 with 3-level priority.

        Priority (highest to lowest):
        1. USER override (user_config.bio_keywords_query1)
        2. ACCOUNT default (account_defaults.bio_keywords_query1)
        3. SYSTEM default (SearchConfig.DEFAULT_BIO_KEYWORDS_QUERY1)

        Session: 2026-02-08 Biographical Keywords Override Fix

        Args:
            user_config: User's bot configuration
            account_defaults: Account-level defaults (optional)

        Returns:
            Resolved biographical keywords for query 1
        """
        from ..domain.settings import SearchConfig

        # Level 1: User override
        if user_config.bio_keywords_query1:
            logger.debug(
                f"📚 Using USER bio_keywords_query1 ({len(user_config.bio_keywords_query1)} keywords)"
            )
            return user_config.bio_keywords_query1

        # Level 2: Account default
        if account_defaults and account_defaults.bio_keywords_query1:
            logger.debug(
                f"📚 Using ACCOUNT bio_keywords_query1 ({len(account_defaults.bio_keywords_query1)} keywords)"
            )
            return account_defaults.bio_keywords_query1

        # Level 3: System default
        search_config = SearchConfig()
        logger.debug(
            f"📚 Using SYSTEM bio_keywords_query1 ({len(search_config.DEFAULT_BIO_KEYWORDS_QUERY1)} keywords)"
        )
        return search_config.DEFAULT_BIO_KEYWORDS_QUERY1

    def get_bio_keywords_query2(
        self,
        user_config: UserBotConfig,
        account_defaults: Optional[UserBotConfig] = None
    ) -> List[str]:
        """
        Resolve biographical keywords query2 with 3-level priority.

        Session: 2026-02-08 Biographical Keywords Override Fix

        Args:
            user_config: User's bot configuration
            account_defaults: Account-level defaults (optional)

        Returns:
            Resolved biographical keywords for query 2
        """
        from ..domain.settings import SearchConfig

        # Level 1: User override
        if user_config.bio_keywords_query2:
            logger.debug(
                f"📚 Using USER bio_keywords_query2 ({len(user_config.bio_keywords_query2)} keywords)"
            )
            return user_config.bio_keywords_query2

        # Level 2: Account default
        if account_defaults and account_defaults.bio_keywords_query2:
            logger.debug(
                f"📚 Using ACCOUNT bio_keywords_query2 ({len(account_defaults.bio_keywords_query2)} keywords)"
            )
            return account_defaults.bio_keywords_query2

        # Level 3: System default
        search_config = SearchConfig()
        logger.debug(
            f"📚 Using SYSTEM bio_keywords_query2 ({len(search_config.DEFAULT_BIO_KEYWORDS_QUERY2)} keywords)"
        )
        return search_config.DEFAULT_BIO_KEYWORDS_QUERY2

    def get_bio_keywords_query3(
        self,
        user_config: UserBotConfig,
        account_defaults: Optional[UserBotConfig] = None
    ) -> List[str]:
        """
        Resolve biographical keywords query3 with 3-level priority.

        Session: 2026-02-08 Biographical Keywords Override Fix

        Args:
            user_config: User's bot configuration
            account_defaults: Account-level defaults (optional)

        Returns:
            Resolved biographical keywords for query 3
        """
        from ..domain.settings import SearchConfig

        # Level 1: User override
        if user_config.bio_keywords_query3:
            logger.debug(
                f"📚 Using USER bio_keywords_query3 ({len(user_config.bio_keywords_query3)} keywords)"
            )
            return user_config.bio_keywords_query3

        # Level 2: Account default
        if account_defaults and account_defaults.bio_keywords_query3:
            logger.debug(
                f"📚 Using ACCOUNT bio_keywords_query3 ({len(account_defaults.bio_keywords_query3)} keywords)"
            )
            return account_defaults.bio_keywords_query3

        # Level 3: System default
        search_config = SearchConfig()
        logger.debug(
            f"📚 Using SYSTEM bio_keywords_query3 ({len(search_config.DEFAULT_BIO_KEYWORDS_QUERY3)} keywords)"
        )
        return search_config.DEFAULT_BIO_KEYWORDS_QUERY3
