# OAuth Multi-Tenant — Complete Guide

**Purpose:** Practical reference for implementing and managing OAuth-based multi-tenant accounts.  
**Audience:** Developers working with Auth, IAM, or Multi-user features.

**Architecture Overview:** See [OAuth Multi-Tenant Building Block](../05_building_blocks/oauth_multi_tenant/README.md)

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [IAM Management](#2-iam-management)
3. [Configuration Inheritance](#3-configuration-inheritance)
4. [Platform Linking](#4-platform-linking)
5. [Session Management](#5-session-management)
6. [Testing & Debugging](#6-testing--debugging)
7. [Code Reference](#7-code-reference)

---

## 1. Quick Start

### 1.1 Register User via Google OAuth

To create a new user (and billing account) via Google OAuth:

```python
# 1. Frontend redirects user to Google
GET /auth/login?provider=firebase

# 2. Google redirects back with code
GET /auth/callback?code=AUTH_CODE&state=CSRF_TOKEN

# 3. Backend response (JSON)
{
    "success": true,
    "user": {
        "user_id": "user_123",
        "email": "user@example.com",
        "external_user_id": "firebase|google_123"
    },
    "account": {
        "account_id": "account_456",
        "tier": "free",
        "role": "owner"
    }
}
```

### 1.2 Login Existing User

Same flow as registration. If `external_user_id` exists, user is logged in instead of created.

### 1.3 Link Slack Account

Link a Slack identity to an existing OAuth user:

```python
from src.services.authentication_service import AuthenticationService

# User must be logged in
await auth_service.link_platform_identity(
    user_id="user_123",
    platform="slack",
    platform_user_id="U123456"
)
```

**Result:** User can now access the bot via Slack (U123456) and Web UI (Google OAuth).

---

## 2. IAM Management

### 2.1 Check User Role

Check what role a user has in their account:

```python
from src.ports.iam_port import IAMPort

iam_service = IAMPort(...)

role = await iam_service.get_user_role(
    user_id="user_123",
    account_id="account_456"
)
# Returns: "owner", "member", or "viewer"
```

### 2.2 Check Permission

Check if user can perform an action:

```python
from src.domain.iam import Action, ResourceType

can_write_fact = await iam_service.can_access_resource(
    user_id="user_123",
    resource_type=ResourceType.FACT,
    action=Action.WRITE,
    resource_id="fact_789"
)

if can_write_fact:
    # Proceed with write
```

### 2.3 Assign Role to Member

Invite a user to an account:

```python
# Add user to account with MEMBER role
await iam_service.assign_role(
    account_id="account_456",
    user_id="user_new",
    role="member"
)

# Update BillingAccount.iam_policy
# {
#   "user_123": "owner",
#   "user_new": "member"
# }
```

### 2.4 Revoke Access

Remove a user from an account:

```python
await iam_service.revoke_access(
    account_id="account_456",
    user_id="user_old"
)
```

**Constraint:** Cannot remove the last OWNER (must assign new owner first).

---

## 3. Configuration Inheritance

### 3.1 Set Account Defaults

Set shared configuration for all team members:

```python
from src.domain.user import UserBotConfig, PerformanceTier

# Set default tier for team
account.account_defaults = UserBotConfig(
    default_tier=PerformanceTier.PERFORMANCE,
    tools_enabled=["search_memory"]
)
await account_repo.update_account(account)
```

### 3.2 Override as User

Individual user overrides account defaults:

```python
# User wants "balanced" tier instead of "performance"
user.config.default_tier = PerformanceTier.BALANCED
await user_repo.update_user(user)
```

### 3.3 Check Effective Config

Get the final merged configuration:

```python
from src.services.configuration_service import ConfigurationService

config_service = ConfigurationService()

# Merge Logic: User > Account > System
effective_config = config_service.merge_configs(
    base=account.account_defaults,
    override=user.config
)

print(effective_config.default_tier)
# Output: PerformanceTier.BALANCED (User override won)
```

---

## 4. Platform Linking

### 4.1 Link Telegram Identity

Enable Telegram access for an existing user:

```python
# 1. User starts Telegram bot
# 2. Bot generates one-time link code: "123-456"
# 3. User enters code in Web UI

# 4. Backend links identity
await auth_service.link_platform_identity(
    user_id="user_123",
    platform="telegram",
    platform_user_id="987654321"
)
```

### 4.2 Unlink Platform

Remove access:

```python
await auth_service.unlink_platform_identity(
    user_id="user_123",
    platform="telegram"
)
```

---

## 5. Session Management

### 5.1 Access Token (JWT)

- **TTL:** 1 hour
- **Storage:** `HttpOnly` cookie (`access_token`)
- **Contents:** `user_id`, `account_id`, `role`, `tier`

**Decoding:**

```python
from src.services.session_service import SessionService

session_service = SessionService(secret="...")
payload = session_service.verify_token(token)
print(payload["user_id"])
```

### 5.2 Refresh Token

- **TTL:** 30 days
- **Storage:** `HttpOnly` cookie (`refresh_token`)
- **Usage:** Exchange for new access token when expired

**Refresh Flow:**

```python
POST /auth/refresh
Cookie: refresh_token=...

Response:
Set-Cookie: access_token=... (new 1h token)
```

### 5.3 Logout

Clears both cookies:

```python
POST /auth/logout

Response:
Set-Cookie: access_token=; Max-Age=0
Set-Cookie: refresh_token=; Max-Age=0
```

---

## 6. Testing & Debugging

### 6.1 Unit Tests

Run auth-related tests:

```bash
# Auth Service
pytest tests/unit/services/test_authentication_service.py -v

# IAM Adapter
pytest tests/unit/adapters/test_firestore_iam_adapter.py -v

# Config Service
pytest tests/unit/services/test_configuration_service.py -v
```

### 6.2 Integration Tests

Run full OAuth flow tests:

```bash
pytest tests/integration/test_oauth_integration.py -v
```

### 6.3 Common Issues

| Issue                   | Cause                      | Solution                                    |
| :---------------------- | :------------------------- | :------------------------------------------ |
| **401 Unauthorized**    | Token expired or missing   | Refresh token or re-login                   |
| **403 Forbidden**       | Insufficient IAM role      | Check role: `owner` required for billing    |
| **Config not merging**  | `account_defaults` is None | Initialize account defaults via admin       |
| **Platform link fails** | ID already linked          | Unlink from other account first             |
| **Firebase Error**      | Invalid credentials        | Check `GOOGLE_APPLICATION_CREDENTIALS` path |

---

## 7. Code Reference

### 7.1 Domain Models

| File                     | Purpose                      |
| :----------------------- | :--------------------------- |
| `src/domain/user.py`     | UserProfile + UserBotConfig  |
| `src/domain/billing.py`  | BillingAccount + IAM Policy  |
| `src/domain/entities.py` | FactEntity (with visibility) |

### 7.2 Services

| File                                     | Purpose                      |
| :--------------------------------------- | :--------------------------- |
| `src/services/authentication_service.py` | Registration, Login, Linking |
| `src/services/session_service.py`        | JWT Token Management         |
| `src/services/configuration_service.py`  | Config Merge Logic           |

### 7.3 Repositories

| File                                        | Purpose                 |
| :------------------------------------------ | :---------------------- |
| `src/adapters/firebase_auth_adapter.py`     | OAuth Provider (Google) |
| `src/adapters/firestore_iam_adapter.py`     | Permission Checks       |
| `src/adapters/firestore_user_repository.py` | User Storage            |

### 7.4 Tests

| File                                                | Purpose           |
| :-------------------------------------------------- | :---------------- |
| `tests/integration/test_oauth_integration.py`       | E2E OAuth Flow    |
| `tests/unit/services/test_configuration_service.py` | Merge Logic Tests |

---

## Related Documentation

- [OAuth Multi-Tenant Building Block](../05_building_blocks/oauth_multi_tenant/README.md) — Architecture
- [OAuth testing guide](../05_building_blocks/oauth_multi_tenant/TESTING_GUIDE.md) — Test procedures

**Last Updated:** 2026-02-05  
**Status:** ✅ Production Ready
