# User Management System: Complete Guide

## 1. Quick Start

### 1.1 Self-Link Flow (Connect Platform)

**Scenario:** A user registered via Google wants to connect their Slack account.

```bash
# 1. Generate Invite Code (Web UI)
curl -X POST http://localhost:5001/api/user/invite-codes \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"type": "self_link", "platform": "slack"}'

# Response:
# {"code": "ABC-XYZ-123", "type": "self_link"}

# 2. Consume Code (Slack)
# User sends message to bot: "ABC-XYZ-123 Hello"
# Bot responds: "✅ Slack account linked successfully!"
```

### 1.2 Team Invite Flow (Add Member)

**Scenario:** Owner invites a colleague to their account.

```bash
# 1. Generate Team Invite (Web UI)
curl -X POST http://localhost:5001/api/user/invite-codes \
  -H "Authorization: Bearer <owner_token>" \
  -d '{"type": "team_invite", "role": "MEMBER"}'

# Response: {"code": "XYZ-789-QWE", "type": "team_invite"}

# 2. Colleague Joins (Web UI)
# Colleague logs in via OAuth, then enters code:
curl -X POST http://localhost:5001/api/user/join-team \
  -H "Authorization: Bearer <colleague_token>" \
  -d '{"code": "XYZ-789-QWE"}'
```

---

## 2. Invite Code System (Phase 1 & 3)

### 2.1 Domain Entity (`src/domain/invite_code.py`)

```python
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

class InviteType(Enum):
    SELF_LINK = "self_link"      # Link platform to YOUR account
    TEAM_INVITE = "team_invite"  # Invite another OAuth user to YOUR account

@dataclass
class InviteCode:
    """
    Represents a secure, time-limited code for linking identities or joining teams.
    """
    code: str                  # Unique identifier (e.g. "ABC-XYZ-123")
    user_id: str               # The user who created this code (Owner)
    account_id: str            # The account this code is tied to
    type: InviteType           # SELF_LINK or TEAM_INVITE
    platform: Optional[str] = None  # Target platform (only for SELF_LINK)
    role: str = "MEMBER"       # Target role (only for TEAM_INVITE)
    expires_at: datetime       # Expiration timestamp (UTC)
    used_at: Optional[datetime] = None  # When the code was consumed
    used_by_user_id: Optional[str] = None # Who consumed the code
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_valid(self) -> bool:
        """Check if code is unused and not expired."""
        now = datetime.now(timezone.utc)
        return self.used_at is None and now < self.expires_at
```

### 2.2 Repository Port (`src/ports/invite_code_repository.py`)

```python
from abc import ABC, abstractmethod
from typing import List, Optional
from ..domain.invite_code import InviteCode

class InviteCodeRepository(ABC):
    @abstractmethod
    async def create(self, invite_code: InviteCode) -> InviteCode:
        pass

    @abstractmethod
    async def get_by_code(self, code: str) -> Optional[InviteCode]:
        pass

    @abstractmethod
    async def mark_used(self, code: str, used_by_user_id: str) -> InviteCode:
        """Mark code as used by specific user ID (idempotent)."""
        pass

    @abstractmethod
    async def list_by_user(self, user_id: str) -> List[InviteCode]:
        pass
```

### 2.3 Firestore Adapter (`src/adapters/firestore_invite_code_repo.py`)

```python
# Standard Firestore implementation mapping Domain <-> Dict
# Key fields: type, platform, role, used_by_user_id
```

### 2.4 Service Logic (`src/services/invite_code_service.py`)

```python
class InviteCodeService:
    def __init__(self, repo: InviteCodeRepository, user_repo, account_repo):
        self.repo = repo
        self.user_repo = user_repo
        self.account_repo = account_repo

    async def generate_self_link(self, user_id: str, account_id: str, platform: str) -> InviteCode:
        return await self._create_code(
            user_id, account_id, InviteType.SELF_LINK, platform=platform
        )

    async def generate_team_invite(self, user_id: str, account_id: str, role: str) -> InviteCode:
        return await self._create_code(
            user_id, account_id, InviteType.TEAM_INVITE, role=role
        )

    async def validate_code(self, code: str) -> InviteCode:
        invite = await self.repo.get_by_code(code)
        if not invite or not invite.is_valid():
            raise ValueError("Invalid or expired invite code")
        return invite

    async def consume_team_invite(self, code: str, new_member_user_id: str):
        """
        Join new member to OWNER's account.
        """
        invite = await self.repo.get_by_code(code)

        # Validation
        if invite.type != InviteType.TEAM_INVITE:
            raise ValueError("Code is not for team invite")
        if not invite.is_valid():
            raise ValueError("Code invalid or expired")

        # Logic: Join Account
        owner_account = await self.account_repo.get_account(invite.account_id)
        new_member = await self.user_repo.get_user(new_member_user_id)
        old_account_id = new_member.account_id

        # 1. Update User
        new_member.account_id = invite.account_id
        await self.user_repo.update_user(new_member)

        # 2. Update IAM
        owner_account.iam_policy[new_member_user_id] = invite.role
        await self.account_repo.update_account(owner_account)

        # 3. Mark Used
        await self.repo.mark_used(code, new_member_user_id)

        # 4. Cleanup old solo account (optional, if empty)
        # await self.account_repo.delete_account(old_account_id)

        return new_member
```

---

## 3. Web UI Integration (Adapters)

### 3.1 Invite Generation Endpoint

```python
@app.route("/api/user/invite-codes", methods=["POST"])
async def create_invite():
    body = await request.get_json()
    invite_type = body.get("type", "self_link")

    # Get current user context
    user_id = g.user_id
    account_id = g.account_id  # From JWT or DB lookup

    if invite_type == "self_link":
        platform = body.get("platform")
        code = await invite_service.generate_self_link(user_id, account_id, platform)

    elif invite_type == "team_invite":
        role = body.get("role", "MEMBER")
        code = await invite_service.generate_team_invite(user_id, account_id, role)

    return jsonify(code.to_dict()), 201
```

### 3.2 Join Team Endpoint

```python
@app.route("/api/user/join-team", methods=["POST"])
async def join_team():
    body = await request.get_json()
    code = body.get("code")

    try:
        updated_user = await invite_service.consume_team_invite(code, g.user_id)
        return jsonify({"success": True, "account_id": updated_user.account_id})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
```

---

## 4. Slack Integration (Self-Link Only)

### 4.1 IdentityResolver Logic

```python
async def resolve_user(self, platform: str, platform_user_id: str, message_text: str = None):
    # 1. Check existing link
    user = await self.user_repo.get_user_by_platform_id(platform, platform_user_id)
    if user: return user

    # 2. Check for SELF_LINK Code
    if message_text:
        code_str = extract_code(message_text)
        if code_str:
            try:
                invite = await self.invite_service.validate_code(code_str)

                # STRICT VALIDATION: Must be SELF_LINK and correct platform
                if invite.type != InviteType.SELF_LINK:
                     raise ValueError("Not a self-link code")
                if invite.platform != platform:
                     raise ValueError("Wrong platform")

                # Link Identity
                owner = await self.user_repo.get_user(invite.user_id)
                owner.platform_identities[platform] = platform_user_id
                await self.user_repo.update_user(owner)

                await self.invite_service.repo.mark_used(code_str, owner.user_id)
                return owner
            except ValueError:
                pass

    # 3. Fallback: Auto-create (Solo Account)
    return await self._register_new_user(platform, platform_user_id)
```

---

## 5. Security Model

| Feature                  | Threat                                              | Mitigation                                                                          |
| :----------------------- | :-------------------------------------------------- | :---------------------------------------------------------------------------------- |
| **Self-Link**            | Attacker links victim's Slack to attacker's account | Code generated in OAuth session; Attacker must have code AND control Slack account. |
| **Team Invite**          | Random user joins Team                              | Code must be generated by OWNER; Consumer must be OAuth authenticated.              |
| **Code Guessing**        | Brute force codes                                   | 48-bit entropy; Rate limiting; 7-day expiry.                                        |
| **Privilege Escalation** | Member invites Admin                                | Invite role is fixed at generation time by Owner.                                   |

---

## 6. Testing Strategy

### 6.1 Integration Tests (`tests/integration/test_invites.py`)

```python
async def test_team_invite_flow():
    # 1. Setup: Owner and NewUser (Solo)
    owner = await create_mock_user("owner")
    new_user = await create_mock_user("new_user")

    # 2. Owner generates code
    code = await invite_service.generate_team_invite(owner.user_id, owner.account_id, "MEMBER")

    # 3. NewUser consumes code
    updated_user = await invite_service.consume_team_invite(code.code, new_user.user_id)

    # 4. Verify
    assert updated_user.account_id == owner.account_id
    account = await account_repo.get_account(owner.account_id)
    assert account.iam_policy[new_user.user_id] == "MEMBER"
```
