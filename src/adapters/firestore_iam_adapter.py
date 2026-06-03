"""
Firestore IAM Adapter (OAuth Multi-Tenant Session 5).

Implements IAMPort using Firestore-backed BillingAccount.iam_policy.
Simple role-based access control (OWNER, MEMBER, VIEWER).

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
from typing import Optional, Dict

from ..ports.iam_port import IAMPort, Role, ResourceType, Action
from ..ports.account_repository import AccountRepository
from ..utils.logger import logger


class FirestoreIAMAdapter(IAMPort):
    """
    Firestore-backed IAM implementation.

    Uses BillingAccount.iam_policy as source of truth:
    - iam_policy: Dict[user_id, role] (e.g., {"user-1": "owner"})

    Permission model:
    1. Get user's role from account IAM policy
    2. Check role permissions via ROLE_PERMISSIONS matrix
    3. For resources with USER_PRIVATE visibility, check creator

    MVP Roles:
    - OWNER: Full control (admin actions, manage members)
    - MEMBER: Read/write shared resources
    - VIEWER: Read-only access
    """

    def __init__(self, account_repo: AccountRepository):
        """
        Initialize IAM adapter.

        Args:
            account_repo: Account repository for reading/writing IAM policies
        """
        self.account_repo = account_repo

    async def can_access_resource(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        action: Action,
        account_id: Optional[str] = None,
    ) -> bool:
        """
        Check if user has permission to perform action on resource.

        Logic:
        1. If no account_id provided, deny (all resources must belong to account)
        2. Get user's role in account from IAM policy
        3. Check if role has permission via ROLE_PERMISSIONS matrix
        4. Return True if permitted, False otherwise

        Args:
            user_id: User attempting the action
            resource_type: Type of resource (ACCOUNT, USER, FACT, etc.)
            resource_id: Specific resource identifier (unused in MVP)
            action: Action to perform (READ, WRITE, DELETE, ADMIN)
            account_id: Account context (required)

        Returns:
            True if user has permission, False otherwise

        Note:
            MVP ignores resource_id - only checks role-based permissions.
            Future: Add resource-level policies (e.g., per-fact permissions).
        """
        if not account_id:
            logger.warning("❌ IAM check failed: no account_id provided")
            return False

        try:
            # Get user's role in account
            role = await self.get_user_role(user_id, account_id)

            if not role:
                logger.debug(
                    f"🔒 Access denied: user {user_id} not member of account {account_id}"
                )
                return False

            # Check if role has permission
            has_perm = self.has_permission(role, resource_type, action)

            if has_perm:
                logger.debug(
                    f"✅ Access granted: user {user_id} ({role.value}) "
                    f"can {action.value} {resource_type.value}"
                )
            else:
                logger.debug(
                    f"🔒 Access denied: role {role.value} cannot {action.value} "
                    f"{resource_type.value}"
                )

            return has_perm

        except Exception as e:
            logger.error(f"❌ IAM check failed: {e}")
            return False

    async def get_user_role(self, user_id: str, account_id: str) -> Optional[Role]:
        """
        Get user's role in specific account.

        Reads from BillingAccount.iam_policy[user_id].

        Args:
            user_id: User identifier
            account_id: Account identifier

        Returns:
            Role enum (OWNER, MEMBER, VIEWER) or None if not a member

        Raises:
            ValueError: If account not found
        """
        try:
            account = await self.account_repo.get_account(account_id)

            if not account:
                raise ValueError(f"Account {account_id} not found")

            # Get role from IAM policy
            role_str = account.iam_policy.get(user_id)

            if not role_str:
                return None

            # Convert string to Role enum
            try:
                role = Role(role_str)
                return role
            except ValueError:
                logger.warning(
                    f"⚠️ Invalid role '{role_str}' for user {user_id} "
                    f"in account {account_id}"
                )
                return None

        except Exception as e:
            logger.error(f"❌ Failed to get user role: {e}")
            raise

    async def assign_role(
        self,
        user_id: str,
        account_id: str,
        role: Role,
        assigned_by: str,
    ) -> bool:
        """
        Assign or update user's role in account.

        Permission check: Only OWNER can assign roles.

        Args:
            user_id: User to assign role to
            account_id: Account identifier
            role: Role to assign (OWNER, MEMBER, VIEWER)
            assigned_by: User performing the assignment

        Returns:
            True if successful

        Raises:
            PermissionError: If assigned_by is not OWNER
            ValueError: If account not found
        """
        try:
            # Permission check: assigned_by must be OWNER
            assigner_role = await self.get_user_role(assigned_by, account_id)

            if assigner_role != Role.OWNER:
                raise PermissionError(
                    f"User {assigned_by} is not OWNER of account {account_id}"
                )

            # Load account
            account = await self.account_repo.get_account(account_id)

            if not account:
                raise ValueError(f"Account {account_id} not found")

            # Update IAM policy
            old_role = account.iam_policy.get(user_id)
            account.iam_policy[user_id] = role.value

            # Persist changes
            await self.account_repo.update_account(account)

            logger.info(
                f"👤 Role assigned: user {user_id} → {role.value} "
                f"in account {account_id} (by {assigned_by})"
                f"{f' (was {old_role})' if old_role else ''}"
            )

            return True

        except PermissionError:
            raise
        except Exception as e:
            logger.error(f"❌ Failed to assign role: {e}")
            raise ValueError(f"Role assignment failed: {e}")

    async def revoke_access(
        self,
        user_id: str,
        account_id: str,
        revoked_by: str,
    ) -> bool:
        """
        Revoke user's access to account.

        Permission checks:
        - Only OWNER can revoke access
        - Cannot revoke if user is the only OWNER
        - Cannot revoke own OWNER access if sole owner

        Args:
            user_id: User to revoke access from
            account_id: Account identifier
            revoked_by: User performing the revocation

        Returns:
            True if successful

        Raises:
            PermissionError: If revoked_by is not OWNER or trying to revoke sole owner
            ValueError: If account not found or user not a member
        """
        try:
            # Permission check: revoked_by must be OWNER
            revoker_role = await self.get_user_role(revoked_by, account_id)

            if revoker_role != Role.OWNER:
                raise PermissionError(
                    f"User {revoked_by} is not OWNER of account {account_id}"
                )

            # Load account
            account = await self.account_repo.get_account(account_id)

            if not account:
                raise ValueError(f"Account {account_id} not found")

            # Check if user is a member
            if user_id not in account.iam_policy:
                raise ValueError(f"User {user_id} is not a member of account {account_id}")

            user_role = Role(account.iam_policy[user_id])

            # Safety check: Cannot revoke if sole OWNER
            if user_role == Role.OWNER:
                owner_count = sum(
                    1 for role in account.iam_policy.values() if role == Role.OWNER.value
                )

                if owner_count == 1:
                    raise PermissionError(
                        f"Cannot revoke sole OWNER of account {account_id}"
                    )

            # Remove from IAM policy
            del account.iam_policy[user_id]

            # Persist changes
            await self.account_repo.update_account(account)

            logger.info(
                f"🚫 Access revoked: user {user_id} removed from account {account_id} "
                f"(by {revoked_by})"
            )

            return True

        except (PermissionError, ValueError):
            raise
        except Exception as e:
            logger.error(f"❌ Failed to revoke access: {e}")
            raise ValueError(f"Access revocation failed: {e}")

    async def get_account_members(self, account_id: str) -> Dict[str, Role]:
        """
        Get all members of an account with their roles.

        Args:
            account_id: Account identifier

        Returns:
            Dictionary mapping user_id → Role enum

        Raises:
            ValueError: If account not found
        """
        try:
            account = await self.account_repo.get_account(account_id)

            if not account:
                raise ValueError(f"Account {account_id} not found")

            # Convert string roles to Role enums
            members = {}
            for user_id, role_str in account.iam_policy.items():
                try:
                    members[user_id] = Role(role_str)
                except ValueError:
                    logger.warning(
                        f"⚠️ Skipping invalid role '{role_str}' for user {user_id}"
                    )

            logger.debug(f"📋 Account {account_id} has {len(members)} members")

            return members

        except Exception as e:
            logger.error(f"❌ Failed to get account members: {e}")
            raise ValueError(f"Failed to get account members: {e}")
