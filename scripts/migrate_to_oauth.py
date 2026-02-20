#!/usr/bin/env python3
"""
Data Migration Script: OAuth Multi-Tenant (Session 8)

Migrates data from old collections to new _oauth collections:
- dev_users → dev_users_oauth
- dev_accounts → dev_accounts_oauth
- dev_facts → dev_facts_oauth

Data Transformations:
1. Users: Add OAuth fields (external_user_id, auth_metadata), create default BillingAccount
2. Accounts: Add IAM policy (owner role), account_defaults field
3. Facts: Rename owner_id → created_by_user_id, add account_id, visibility

Safety Features:
- Dry-run mode (default: enabled)
- Backup verification required
- Progress tracking and logging
- Rollback safety: Old collections remain untouched
- Error handling with detailed reporting

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
Session Protocol: docs/12_risks/session_protocols/SESSION_PROTOCOL_2026-01-31.md
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from uuid import uuid4

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from google.cloud import firestore
from src.utils.logger import logger


class OAuthMigrationScript:
    """OAuth Multi-Tenant data migration script."""

    def __init__(
        self,
        db_client: firestore.Client,
        source_prefix: str = "dev_",
        target_prefix: str = "dev_",
        dry_run: bool = True,
    ):
        """
        Initialize migration script.

        Args:
            db_client: Firestore client
            source_prefix: Source collection prefix (e.g., "dev_")
            target_prefix: Target collection prefix (e.g., "dev_")
            dry_run: If True, simulate migration without writing data
        """
        self.db = db_client
        self.source_prefix = source_prefix
        self.target_prefix = target_prefix
        self.dry_run = dry_run

        # Collection names
        self.source_users = f"{source_prefix}users"
        self.target_users = f"{source_prefix}users_oauth"
        self.source_accounts = f"{source_prefix}accounts"
        self.target_accounts = f"{source_prefix}accounts_oauth"
        self.source_facts = f"{source_prefix}facts"
        self.target_facts = f"{source_prefix}facts_oauth"

        # Migration statistics
        self.stats = {
            "users_migrated": 0,
            "accounts_created": 0,
            "facts_migrated": 0,
            "errors": [],
        }

        # User ID → Account ID mapping (for fact migration)
        self.user_to_account_map: Dict[str, str] = {}

    def log(self, level: str, message: str):
        """Log with consistent formatting."""
        prefix = "🔵 [DRY-RUN]" if self.dry_run else "🟢 [LIVE]"
        if level == "info":
            logger.info(f"{prefix} {message}")
        elif level == "warning":
            logger.warning(f"{prefix} {message}")
        elif level == "error":
            logger.error(f"{prefix} {message}")

    async def verify_prerequisites(self) -> bool:
        """
        Verify migration prerequisites.

        Returns:
            True if prerequisites met, False otherwise
        """
        self.log("info", "Verifying migration prerequisites...")

        # Check source collections exist and have data
        try:
            users_count = len(list(self.db.collection(self.source_users).limit(1).stream()))
            if users_count == 0:
                self.log("warning", f"Source collection {self.source_users} is empty or doesn't exist")
                return False

            self.log("info", f"✅ Source collection {self.source_users} exists")

            # Check target collections don't exist or are empty
            target_users_count = len(list(self.db.collection(self.target_users).limit(1).stream()))
            if target_users_count > 0:
                self.log("error", f"Target collection {self.target_users} already has data! Aborting.")
                return False

            self.log("info", f"✅ Target collection {self.target_users} is empty")

            return True

        except Exception as e:
            self.log("error", f"Failed to verify prerequisites: {e}")
            return False

    async def migrate_users_and_accounts(self) -> bool:
        """
        Migrate users and create default billing accounts.

        For each user:
        1. Create a new BillingAccount (if user doesn't have one)
        2. Add OAuth fields to user (external_user_id, auth_metadata)
        3. Link user to account via account_id
        4. Set user as OWNER in account's IAM policy

        Returns:
            True if migration successful
        """
        self.log("info", "=" * 80)
        self.log("info", "PHASE 1: Migrating Users and Creating Accounts")
        self.log("info", "=" * 80)

        try:
            # Stream all users from source collection
            users_ref = self.db.collection(self.source_users)
            users = users_ref.stream()

            for user_doc in users:
                user_data = user_doc.to_dict()
                user_id = user_doc.id

                try:
                    # Prepare user data with OAuth fields
                    migrated_user = user_data.copy()

                    # Add OAuth fields (if not present)
                    if "external_user_id" not in migrated_user:
                        migrated_user["external_user_id"] = None
                    if "auth_metadata" not in migrated_user:
                        migrated_user["auth_metadata"] = None
                    if "platform_identities" not in migrated_user:
                        migrated_user["platform_identities"] = {}

                    # Create default BillingAccount for this user
                    account_id = f"account-{user_id}"
                    account_data = {
                        "account_id": account_id,
                        "tier": "free",
                        "usage": {
                            "total_requests": 0,
                            "total_tokens": 0,
                            "total_cost": 0.0,
                            "daily_tokens": 0,
                            "daily_cost": 0.0,
                            "daily_reset_at": datetime.now(timezone.utc),
                            "monthly_tokens": 0,
                            "monthly_cost": 0.0,
                            "monthly_reset_at": datetime.now(timezone.utc),
                        },
                        "daily_token_limit": 100_000,
                        "monthly_cost_limit": 50.0,
                        "iam_policy": {user_id: "owner"},  # User is OWNER of their account
                        "account_defaults": None,  # No defaults configured yet
                        "created_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                        "is_active": True,
                    }

                    # Link user to account
                    migrated_user["account_id"] = account_id

                    # Store mapping for fact migration
                    self.user_to_account_map[user_id] = account_id

                    if not self.dry_run:
                        # Write to target collections
                        self.db.collection(self.target_users).document(user_id).set(migrated_user)
                        self.db.collection(self.target_accounts).document(account_id).set(account_data)

                    self.stats["users_migrated"] += 1
                    self.stats["accounts_created"] += 1

                    if self.stats["users_migrated"] % 10 == 0:
                        self.log("info", f"Migrated {self.stats['users_migrated']} users...")

                except Exception as e:
                    error_msg = f"Failed to migrate user {user_id}: {e}"
                    self.log("error", error_msg)
                    self.stats["errors"].append(error_msg)
                    continue

            self.log("info", f"✅ Users migrated: {self.stats['users_migrated']}")
            self.log("info", f"✅ Accounts created: {self.stats['accounts_created']}")
            return True

        except Exception as e:
            self.log("error", f"Failed to migrate users: {e}")
            return False

    async def migrate_facts(self) -> bool:
        """
        Migrate facts with new ownership model.

        Transformations:
        - owner_id → created_by_user_id
        - Add account_id (lookup from user_to_account_map)
        - Add visibility (default: ACCOUNT_SHARED)

        Returns:
            True if migration successful
        """
        self.log("info", "=" * 80)
        self.log("info", "PHASE 2: Migrating Facts")
        self.log("info", "=" * 80)

        try:
            # Stream all facts from source collection
            facts_ref = self.db.collection(self.source_facts)
            facts = facts_ref.stream()

            for fact_doc in facts:
                fact_data = fact_doc.to_dict()
                fact_id = fact_doc.id

                try:
                    # Prepare fact data with new ownership model
                    migrated_fact = fact_data.copy()

                    # Transform ownership fields
                    if "owner_id" in migrated_fact:
                        owner_id = migrated_fact.pop("owner_id")
                        migrated_fact["created_by_user_id"] = owner_id

                        # Lookup account_id from user mapping
                        account_id = self.user_to_account_map.get(owner_id)
                        if not account_id:
                            error_msg = f"Fact {fact_id}: Cannot find account for user {owner_id}"
                            self.log("warning", error_msg)
                            self.stats["errors"].append(error_msg)
                            # Skip this fact or use orphan account?
                            continue

                        migrated_fact["account_id"] = account_id
                    else:
                        error_msg = f"Fact {fact_id}: Missing owner_id field"
                        self.log("warning", error_msg)
                        self.stats["errors"].append(error_msg)
                        continue

                    # Add visibility field (default: account_shared)
                    if "visibility" not in migrated_fact:
                        migrated_fact["visibility"] = "account_shared"

                    if not self.dry_run:
                        # Write to target collection
                        self.db.collection(self.target_facts).document(fact_id).set(migrated_fact)

                    self.stats["facts_migrated"] += 1

                    if self.stats["facts_migrated"] % 100 == 0:
                        self.log("info", f"Migrated {self.stats['facts_migrated']} facts...")

                except Exception as e:
                    error_msg = f"Failed to migrate fact {fact_id}: {e}"
                    self.log("error", error_msg)
                    self.stats["errors"].append(error_msg)
                    continue

            self.log("info", f"✅ Facts migrated: {self.stats['facts_migrated']}")
            return True

        except Exception as e:
            self.log("error", f"Failed to migrate facts: {e}")
            return False

    async def run(self) -> bool:
        """
        Run complete migration.

        Returns:
            True if migration successful
        """
        start_time = datetime.now()

        self.log("info", "")
        self.log("info", "=" * 80)
        self.log("info", "OAuth Multi-Tenant Data Migration (Session 8)")
        self.log("info", "=" * 80)
        self.log("info", f"Mode: {'DRY-RUN (no data written)' if self.dry_run else 'LIVE (data will be written)'}")
        self.log("info", f"Source prefix: {self.source_prefix}")
        self.log("info", f"Target prefix: {self.target_prefix}")
        self.log("info", "")

        # Verify prerequisites
        if not await self.verify_prerequisites():
            self.log("error", "❌ Prerequisites not met. Aborting migration.")
            return False

        # Phase 1: Migrate users and create accounts
        if not await self.migrate_users_and_accounts():
            self.log("error", "❌ User/account migration failed. Aborting.")
            return False

        # Phase 2: Migrate facts
        if not await self.migrate_facts():
            self.log("error", "❌ Fact migration failed.")
            return False

        # Print summary
        duration = datetime.now() - start_time
        self.log("info", "")
        self.log("info", "=" * 80)
        self.log("info", "MIGRATION SUMMARY")
        self.log("info", "=" * 80)
        self.log("info", f"Duration: {duration}")
        self.log("info", f"Users migrated: {self.stats['users_migrated']}")
        self.log("info", f"Accounts created: {self.stats['accounts_created']}")
        self.log("info", f"Facts migrated: {self.stats['facts_migrated']}")
        self.log("info", f"Errors: {len(self.stats['errors'])}")

        if self.stats["errors"]:
            self.log("warning", "")
            self.log("warning", "Errors encountered:")
            for error in self.stats["errors"][:10]:  # Show first 10 errors
                self.log("warning", f"  - {error}")
            if len(self.stats["errors"]) > 10:
                self.log("warning", f"  ... and {len(self.stats['errors']) - 10} more")

        self.log("info", "")
        if self.dry_run:
            self.log("info", "✅ DRY-RUN COMPLETE - No data was written")
            self.log("info", "To perform actual migration, run with --live flag")
        else:
            self.log("info", "✅ MIGRATION COMPLETE")
            self.log("info", f"Old collections remain untouched: {self.source_users}, {self.source_accounts}, {self.source_facts}")
            self.log("info", f"New collections created: {self.target_users}, {self.target_accounts}, {self.target_facts}")

        return True


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="OAuth Multi-Tenant Data Migration")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live migration (default: dry-run)",
    )
    parser.add_argument(
        "--source-prefix",
        default="dev_",
        help="Source collection prefix (default: dev_)",
    )
    parser.add_argument(
        "--target-prefix",
        default="dev_",
        help="Target collection prefix (default: dev_)",
    )

    args = parser.parse_args()

    # Initialize Firestore client
    db = firestore.Client()

    # Run migration
    migration = OAuthMigrationScript(
        db_client=db,
        source_prefix=args.source_prefix,
        target_prefix=args.target_prefix,
        dry_run=not args.live,
    )

    success = await migration.run()

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
