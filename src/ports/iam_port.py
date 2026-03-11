"""
IAM (Identity and Access Management) Port.

Defines role-based access control interface for multi-tenant architecture.
Based on simple role-based permissions (owner, member, viewer).

Adapters:
- FirestoreIAMAdapter (MVP) - src/adapters/firestore_iam_adapter.py
- Future: External IAM providers (Okta, Auth0, AWS IAM)

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, List, Dict


class ResourceType(str, Enum):
    """
    Resource types in the system that require access control.
    """
    ACCOUNT = "account"  # BillingAccount entity
    USER = "user"  # UserProfile entity
    FACT = "fact"  # FactEntity (memory/knowledge base)
    SESSION = "session"  # Conversation session
    CONFIG = "config"  # User/Account configuration


class Action(str, Enum):
    """
    Actions that can be performed on resources.
    Based on CRUD + admin operations.
    """
    READ = "read"  # View resource
    WRITE = "write"  # Create or update resource
    DELETE = "delete"  # Delete resource
    ADMIN = "admin"  # Administrative actions (manage members, IAM policy)


class Role(str, Enum):
    """
    Predefined roles in MVP.

    Role hierarchy (descending permissions):
    - OWNER: Full control (admin actions, manage members, delete account)
    - MEMBER: Read/write access to shared resources (facts, configs)
    - VIEWER: Read-only access to shared resources
    """
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"


# Role → Action permissions matrix (MVP)
ROLE_PERMISSIONS: Dict[Role, Dict[ResourceType, List[Action]]] = {
    Role.OWNER: {
        ResourceType.ACCOUNT: [Action.READ, Action.WRITE, Action.DELETE, Action.ADMIN],
        ResourceType.USER: [Action.READ, Action.WRITE, Action.DELETE],
        ResourceType.FACT: [Action.READ, Action.WRITE, Action.DELETE],
        ResourceType.SESSION: [Action.READ, Action.WRITE, Action.DELETE],
        ResourceType.CONFIG: [Action.READ, Action.WRITE, Action.ADMIN],
    },
    Role.MEMBER: {
        ResourceType.ACCOUNT: [Action.READ],
        ResourceType.USER: [Action.READ],
        ResourceType.FACT: [Action.READ, Action.WRITE],  # Can create/edit shared facts
        ResourceType.SESSION: [Action.READ, Action.WRITE],  # Own sessions only
        ResourceType.CONFIG: [Action.READ, Action.WRITE],  # Own config only
    },
    Role.VIEWER: {
        ResourceType.ACCOUNT: [Action.READ],
        ResourceType.USER: [Action.READ],
        ResourceType.FACT: [Action.READ],  # Read-only access to shared facts
        ResourceType.SESSION: [Action.READ],  # View own sessions
        ResourceType.CONFIG: [Action.READ],  # View own config
    },
}


class IAMPort(ABC):
    """
    IAM (Identity and Access Management) Port.

    Role-based access control for multi-tenant architecture.
    MVP uses simple role enum (owner, member, viewer).

    Future phases may add:
    - Fine-grained permissions (custom roles)
    - Resource-level policies (per-fact permissions)
    - External IAM integration (Okta, Auth0)
    """

    @abstractmethod
    async def can_access_resource(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        action: Action,
        account_id: Optional[str] = None
    ) -> bool:
        """
        Check if user has permission to perform action on resource.

        Args:
            user_id: User attempting the action
            resource_type: Type of resource (ACCOUNT, USER, FACT, etc.)
            resource_id: Specific resource identifier
            action: Action to perform (READ, WRITE, DELETE, ADMIN)
            account_id: Optional account context (if resource belongs to account)

        Returns:
            True if user has permission, False otherwise

        Logic (MVP):
        1. Get user's role in account (via iam_policy)
        2. Check if role has permission for action on resource_type
        3. For USER_PRIVATE facts: also check if user is creator

        Examples:
            can_access_resource(
                user_id="user-1",
                resource_type=ResourceType.FACT,
                resource_id="fact-123",
                action=Action.READ,
                account_id="acc-1"
            ) → True if user is member of acc-1

            can_access_resource(
                user_id="user-2",
                resource_type=ResourceType.ACCOUNT,
                resource_id="acc-1",
                action=Action.ADMIN
            ) → True only if user-2 is owner of acc-1
        """
        pass

    @abstractmethod
    async def get_user_role(self, user_id: str, account_id: str) -> Optional[Role]:
        """
        Get user's role in specific account.

        Args:
            user_id: User identifier
            account_id: Account identifier

        Returns:
            Role enum (OWNER, MEMBER, VIEWER) or None if not a member

        Note:
            Reads from BillingAccount.iam_policy[user_id]
        """
        pass

    @abstractmethod
    async def assign_role(
        self,
        user_id: str,
        account_id: str,
        role: Role,
        assigned_by: str
    ) -> bool:
        """
        Assign or update user's role in account.

        Args:
            user_id: User to assign role to
            account_id: Account identifier
            role: Role to assign (OWNER, MEMBER, VIEWER)
            assigned_by: User performing the assignment (must be OWNER)

        Returns:
            True if successful, False otherwise

        Raises:
            PermissionError: If assigned_by is not OWNER
            ValueError: If user_id or account_id not found

        Note:
            Updates BillingAccount.iam_policy[user_id] = role
        """
        pass

    @abstractmethod
    async def revoke_access(
        self,
        user_id: str,
        account_id: str,
        revoked_by: str
    ) -> bool:
        """
        Revoke user's access to account (remove from IAM policy).

        Args:
            user_id: User to revoke access from
            account_id: Account identifier
            revoked_by: User performing the revocation (must be OWNER)

        Returns:
            True if successful, False otherwise

        Raises:
            PermissionError: If revoked_by is not OWNER or trying to revoke owner
            ValueError: If user_id or account_id not found

        Note:
            Removes user_id from BillingAccount.iam_policy
            Cannot revoke access if user is the only OWNER
        """
        pass

    @abstractmethod
    async def get_account_members(self, account_id: str) -> Dict[str, Role]:
        """
        Get all members of an account with their roles.

        Args:
            account_id: Account identifier

        Returns:
            Dictionary mapping user_id → Role

        Example:
            {
                "user-1": Role.OWNER,
                "user-2": Role.MEMBER,
                "user-3": Role.VIEWER
            }

        Note:
            Returns BillingAccount.iam_policy directly
        """
        pass

    def has_permission(self, role: Role, resource_type: ResourceType, action: Action) -> bool:
        """
        Check if role has permission for action on resource type.

        Helper method using ROLE_PERMISSIONS matrix.
        Can be called synchronously (no DB access).

        Args:
            role: User's role in account
            resource_type: Type of resource
            action: Action to check

        Returns:
            True if role has permission, False otherwise
        """
        permissions = ROLE_PERMISSIONS.get(role, {})
        allowed_actions = permissions.get(resource_type, [])
        return action in allowed_actions
