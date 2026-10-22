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
    Port for account roles: BillingAccount.iam_policy maps user_id to a Role value.

    No caller in src/: nothing wires FirestoreIAMAdapter (the Cabinet reads iam_policy in session_service.py:92; the
    chat gate is IAMService, a PlatformAuthPort). Every line below is the observed contract of FirestoreIAMAdapter.

    Holds for every method:
    - Tenancy: a role is read from the named account only; a role in one account grants nothing in another.
    - Legacy documents: a stored role that is not a Role value (case-sensitive, e.g. "MEMBER" as written by
      invite_code_service.py:147) reads as no role.
    - Atomicity: writes read the account, change iam_policy and replace the whole account document
      (AccountRepository.update_account); not transactional, so a concurrent write to the account is lost.
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
        """True when the user's role in `account_id` allows `action` on `resource_type` (ROLE_PERMISSIONS).

        Refusals: no account_id, no or unknown role, a missing account and any backend error return False; never
            raises (fails closed).
        resource_id is ignored: no per-resource or creator check (observed).
        """
        pass

    @abstractmethod
    async def get_user_role(self, user_id: str, account_id: str) -> Optional[Role]:
        """Return the user's Role in `account_id`, or None when not a member.

        Errors: a missing account raises ValueError; backend errors propagate.
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
        """Set the user's role in `account_id` to `role` and return True; never returns False.

        Refusals: `assigned_by` not OWNER of the account raises PermissionError.
        Errors: a missing account or a backend error raises ValueError. The user's existence is not checked, and an
            owner may change their own role, also as sole owner (observed).
        Idempotency: assigning the current role rewrites it.
        """
        pass

    @abstractmethod
    async def revoke_access(
        self,
        user_id: str,
        account_id: str,
        revoked_by: str
    ) -> bool:
        """Remove the user from `account_id`'s iam_policy and return True; never returns False.

        Refusals: `revoked_by` not OWNER, or the user the sole OWNER, raises PermissionError.
        Errors: a user not in iam_policy (not idempotent), a missing account or a backend error raises ValueError.
        """
        pass

    @abstractmethod
    async def get_account_members(self, account_id: str) -> Dict[str, Role]:
        """Return {user_id: Role} for `account_id`; an entry whose role is not a Role value is omitted.

        Order: none. Errors: a missing account or a backend error raises ValueError.
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
