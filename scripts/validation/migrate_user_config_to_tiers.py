"""
User Config Migration: Legacy → Tier-Based
==========================================

Migrates user configurations from legacy provider/model fields
to new tier-based architecture.

Usage:
    # Dry-run (preview only)
    python scripts/validation/migrate_user_config_to_tiers.py --env development --dry-run
    
    # Execute migration
    python scripts/validation/migrate_user_config_to_tiers.py --env development
    
    # Production migration with backup
    python scripts/validation/migrate_user_config_to_tiers.py --env production --backup

Plan: docs/architecture/provider_refactor/POST_AUDIT_EXECUTION_PLAN.md Session 21
"""

import os
import sys

# Add src to python path
sys.path.append(os.getcwd())

import asyncio
import argparse
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

from src.config.environment import EnvironmentConfig
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.user import UserBotConfig, PerformanceTier, LLMProvider
from src.utils.logger import logger


# ============================================================================
# MAPPING TABLES
# ============================================================================

# Legacy tier names → New tier names
TIER_MAPPING = {
    "FLASH": PerformanceTier.ECO,
    "PRO": PerformanceTier.BALANCED,
    "ULTRA": PerformanceTier.PERFORMANCE,
    "ECO": PerformanceTier.ECO,
    "BALANCED": PerformanceTier.BALANCED,
    "PERFORMANCE": PerformanceTier.PERFORMANCE,
}

# Legacy model names → Tier inference (Gemini)
MODEL_TO_TIER_GEMINI = {
    "gemini-3-flash-preview": PerformanceTier.ECO,
    "gemini-flash-lite-latest": PerformanceTier.ECO,
    "gemini-flash-latest": PerformanceTier.BALANCED,
    "gemini-exp-1206": PerformanceTier.BALANCED,
    "gemini-3-pro-preview": PerformanceTier.PERFORMANCE,
    "models/gemini-3-pro-preview": PerformanceTier.PERFORMANCE,
}

# Legacy model names → Tier inference (Claude)
MODEL_TO_TIER_CLAUDE = {
    "claude-haiku-4-5": PerformanceTier.ECO,
    "claude-sonnet-4-5": PerformanceTier.BALANCED,
    "claude-opus-4-6": PerformanceTier.PERFORMANCE,
}


def detect_provider(model_name: str) -> str:
    """Detect provider from model name string."""
    if not model_name:
        return "gemini"  # default
    
    model_lower = model_name.lower()
    if "gemini" in model_lower:
        return "gemini"
    elif "claude" in model_lower:
        return "claude"
    elif "gpt" in model_lower or "openai" in model_lower:
        return "openai"
    
    return "gemini"  # default


# ============================================================================
# CONFIG MIGRATOR
# ============================================================================

class ConfigMigrator:
    """Migrates UserBotConfig to tier-based architecture."""
    
    def __init__(self, env: str, dry_run: bool = False, backup: bool = True):
        self.env = env
        self.dry_run = dry_run
        self.backup = backup
        self.env_config = EnvironmentConfig()
        self.user_repo = None  # Initialized in run()
        
        self.stats = {
            "total": 0,
            "migrated": 0,
            "skipped": 0,
            "errors": 0
        }
    
    async def run(self):
        """Execute migration."""
        # Initialize repositories
        from google.cloud import firestore
        db = firestore.Client()
        account_repo = FirestoreAccountRepository(db, self.env_config)
        self.user_repo = FirestoreUserRepository(db, self.env_config, account_repo)
        self.db = db
        
        logger.info(f"🚀 Starting migration (env={self.env}, dry_run={self.dry_run})")
        
        # Get all users from Firestore directly (synchronous)
        users_collection = db.collection(f"{self.env_config.firestore_collection_prefix}users")
        user_docs = list(users_collection.stream())
        
        self.stats["total"] = len(user_docs)
        logger.info(f"📊 Found {len(user_docs)} users to process")
        
        # Process each user document
        for doc in user_docs:
            try:
                await self._migrate_user_doc(doc)
            except Exception as e:
                logger.error(f"❌ Error migrating user {doc.id}: {e}", exc_info=True)
                self.stats["errors"] += 1
        
        # Print summary
        self._print_summary()
    
    async def _migrate_user_doc(self, doc):
        """Migrate single user config from Firestore document."""
        user_id = doc.id
        user_data = doc.to_dict()
        
        # Parse config from document
        config_data = user_data.get('config', {})
        config = UserBotConfig(**config_data) if config_data else UserBotConfig()
        
        # Check if already migrated (has new tier-based fields)
        if self._is_already_migrated(config):
            logger.info(f"⏭️  User {user_id[:8]} already migrated (has default_tier)")
            self.stats["skipped"] += 1
            return
        
        # Backup original config
        if self.backup and not self.dry_run:
            await self._backup_config(user_id, config)
        
        # Build new config
        new_config_dict = self._build_new_config(config, user_id)
        
        # Preview
        logger.info(f"🔄 Migrating user {user_id[:8]}:")
        logger.info(f"   OLD: light_provider={self._get_field(config, 'light_llm_provider')}, "
                   f"smart_provider={self._get_field(config, 'smart_llm_provider')}")
        logger.info(f"   NEW: provider_preference={new_config_dict.get('provider_preference')}, "
                   f"default_tier={new_config_dict.get('default_tier')}")
        
        if not self.dry_run:
            # Write new config directly to Firestore
            user_ref = self.db.collection(f"{self.env_config.firestore_collection_prefix}users").document(user_id)
            user_ref.update({'config': new_config_dict})
            logger.info(f"✅ User {user_id[:8]} migrated successfully")
        else:
            logger.info(f"🔍 DRY-RUN: Would migrate user {user_id[:8]}")
        
        self.stats["migrated"] += 1
    
    def _is_already_migrated(self, config: UserBotConfig) -> bool:
        """Check if config has new tier-based fields."""
        # If default_tier exists and is set, consider it migrated
        if hasattr(config, 'default_tier') and config.default_tier:
            return True
        return False
    
    def _get_field(self, config: UserBotConfig, field_name: str) -> Any:
        """Safely get field from config."""
        return getattr(config, field_name, None)
    
    def _build_new_config(self, old_config: UserBotConfig, user_id: str) -> Dict[str, Any]:
        """Transform legacy config to new format."""
        new_config = {}
        
        # 1. Infer default tier from legacy model/tier
        default_tier = self._infer_default_tier(old_config)
        new_config['default_tier'] = default_tier
        
        # 2. Provider preference (unified from light/smart providers)
        provider_preference = self._infer_provider_preference(old_config)
        new_config['provider_preference'] = provider_preference
        
        # 3. Agent-specific tiers (if different from default)
        agent_tiers = self._infer_agent_tiers(old_config, default_tier)
        new_config['agent_tiers'] = agent_tiers if agent_tiers else {}
        
        # 4. Model overrides (for power users with explicit model names)
        model_overrides = self._infer_model_overrides(old_config)
        new_config['model_overrides'] = model_overrides if model_overrides else {}
        
        # 5. Preserve other fields
        if hasattr(old_config, 'prompt_preferences') and old_config.prompt_preferences:
            new_config['prompt_preferences'] = old_config.prompt_preferences
        
        return new_config
    
    def _infer_default_tier(self, config: UserBotConfig) -> PerformanceTier:
        """Infer default tier from legacy configuration."""
        # Try to get from smart_model (primary indicator of user tier preference)
        smart_model = self._get_field(config, 'smart_model')
        if smart_model:
            provider = detect_provider(smart_model)
            if provider == "gemini":
                return MODEL_TO_TIER_GEMINI.get(smart_model, PerformanceTier.BALANCED)
            elif provider == "claude":
                return MODEL_TO_TIER_CLAUDE.get(smart_model, PerformanceTier.BALANCED)
        
        # Fallback: try light_model
        light_model = self._get_field(config, 'light_model')
        if light_model:
            provider = detect_provider(light_model)
            if provider == "gemini":
                return MODEL_TO_TIER_GEMINI.get(light_model, PerformanceTier.ECO)
            elif provider == "claude":
                return MODEL_TO_TIER_CLAUDE.get(light_model, PerformanceTier.ECO)
        
        # Default: BALANCED (middle ground)
        return PerformanceTier.BALANCED
    
    def _infer_provider_preference(self, config: UserBotConfig) -> Optional[str]:
        """Infer unified provider preference from legacy fields."""
        smart_provider = self._get_field(config, 'smart_llm_provider')
        light_provider = self._get_field(config, 'light_llm_provider')
        
        # If both providers are the same, that's the preference
        if smart_provider and light_provider and smart_provider == light_provider:
            return self._normalize_provider(smart_provider)
        
        # If only smart_provider set, use that (smart is primary)
        if smart_provider:
            return self._normalize_provider(smart_provider)
        
        # If only light_provider set, use that
        if light_provider:
            return self._normalize_provider(light_provider)
        
        # No preference (let strategies decide)
        return None
    
    def _normalize_provider(self, provider: Any) -> Optional[str]:
        """Normalize provider enum/string to lowercase name."""
        if provider is None:
            return None
        
        provider_map = {
            LLMProvider.GEMINI: "gemini",
            LLMProvider.ANTHROPIC: "claude",
            LLMProvider.OPENAI: "openai",
            "GEMINI": "gemini",
            "ANTHROPIC": "claude",
            "OPENAI": "openai",
            "gemini": "gemini",
            "anthropic": "claude",
            "claude": "claude",
            "openai": "openai",
        }
        
        # Handle both enum and string
        provider_key = provider.value if hasattr(provider, 'value') else str(provider)
        return provider_map.get(provider_key, "gemini")
    
    def _infer_agent_tiers(self, config: UserBotConfig, default_tier: PerformanceTier) -> Dict[str, PerformanceTier]:
        """Infer agent-specific tier overrides."""
        agent_tiers = {}
        
        # Check if light and smart models imply different tiers
        light_model = self._get_field(config, 'light_model')
        smart_model = self._get_field(config, 'smart_model')
        
        if light_model:
            light_tier = self._tier_from_model(light_model)
            if light_tier != default_tier:
                agent_tiers['quick'] = light_tier
                agent_tiers['router'] = light_tier  # Router uses light model
        
        if smart_model:
            smart_tier = self._tier_from_model(smart_model)
            if smart_tier != default_tier:
                agent_tiers['smart'] = smart_tier
                agent_tiers['consolidation'] = smart_tier  # Consolidation uses smart model
        
        return agent_tiers
    
    def _tier_from_model(self, model_name: str) -> PerformanceTier:
        """Get tier from model name."""
        provider = detect_provider(model_name)
        if provider == "gemini":
            return MODEL_TO_TIER_GEMINI.get(model_name, PerformanceTier.BALANCED)
        elif provider == "claude":
            return MODEL_TO_TIER_CLAUDE.get(model_name, PerformanceTier.BALANCED)
        return PerformanceTier.BALANCED
    
    def _infer_model_overrides(self, config: UserBotConfig) -> Dict[str, str]:
        """Preserve explicit model overrides for power users."""
        model_overrides = {}
        
        # Preserve smart_model if explicitly set
        smart_model = self._get_field(config, 'smart_model')
        if smart_model:
            model_overrides['smart'] = smart_model
            # Consolidation uses same as smart
            model_overrides['consolidation'] = smart_model
        
        # Preserve light_model if explicitly set
        light_model = self._get_field(config, 'light_model')
        if light_model:
            model_overrides['quick'] = light_model
            model_overrides['router'] = light_model
        
        return model_overrides
    
    async def _backup_config(self, user_id: str, config: UserBotConfig):
        """Backup original config to separate collection."""
        from google.cloud import firestore
        
        try:
            backup_data = {
                "user_id": user_id,
                "config": self._config_to_dict(config),
                "backup_timestamp": datetime.utcnow().isoformat(),
                "migration_version": "tier_based_v1",
                "environment": self.env
            }
            
            # Write to config_backups collection
            db = firestore.Client()
            backup_ref = db.collection(f"{self.env_config.firestore_collection_prefix}config_backups")
            backup_ref.document(user_id).set(backup_data)
            
            logger.debug(f"💾 Backed up config for {user_id[:8]}")
        except Exception as e:
            logger.error(f"❌ Failed to backup config for {user_id}: {e}")
            # Don't fail migration on backup error, but log it
    
    def _config_to_dict(self, config: UserBotConfig) -> Dict[str, Any]:
        """Convert config to dict for backup."""
        if hasattr(config, 'model_dump'):
            return config.model_dump()
        elif hasattr(config, 'dict'):
            return config.dict()
        else:
            return config.__dict__
    
    def _print_summary(self):
        """Print migration summary."""
        logger.info("="*60)
        logger.info("📊 MIGRATION SUMMARY")
        logger.info("="*60)
        logger.info(f"Environment:    {self.env}")
        logger.info(f"Total users:    {self.stats['total']}")
        logger.info(f"Migrated:       {self.stats['migrated']}")
        logger.info(f"Skipped:        {self.stats['skipped']}")
        logger.info(f"Errors:         {self.stats['errors']}")
        logger.info(f"Mode:           {'DRY-RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Backup:         {'ENABLED' if self.backup else 'DISABLED'}")
        logger.info("="*60)
        
        if self.stats['errors'] > 0:
            logger.warning(f"⚠️  {self.stats['errors']} errors occurred. Check logs above.")
        
        if self.dry_run:
            logger.info("🔍 This was a DRY-RUN. No changes were made.")
            logger.info("   Remove --dry-run to execute migration.")


async def main():
    parser = argparse.ArgumentParser(
        description="Migrate user configs to tier-based architecture",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview changes in development
  python scripts/validation/migrate_user_config_to_tiers.py --env development --dry-run
  
  # Execute migration in development
  python scripts/validation/migrate_user_config_to_tiers.py --env development
  
  # Production migration with backup
  python scripts/validation/migrate_user_config_to_tiers.py --env production --backup
        """
    )
    parser.add_argument(
        "--env",
        choices=["development", "production"],
        required=True,
        help="Environment to migrate"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying (recommended first)"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        default=True,
        help="Backup configs before migration (default: enabled)"
    )
    parser.add_argument(
        "--no-backup",
        dest="backup",
        action="store_false",
        help="Disable backup (not recommended for production)"
    )
    
    args = parser.parse_args()
    
    # Safety check for production
    if args.env == "production" and not args.dry_run and not args.backup:
        logger.error("❌ Production migration without backup is not allowed!")
        logger.error("   Remove --no-backup or add --dry-run")
        return 1
    
    migrator = ConfigMigrator(
        env=args.env,
        dry_run=args.dry_run,
        backup=args.backup
    )
    
    await migrator.run()
    
    return 0 if migrator.stats['errors'] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
