# User Management System (Building Block)

## 1. Overview

The **User Management System** handles the entire lifecycle of an Alek-Core user, from initial registration via Google OAuth to linking multiple messaging platforms and managing team access. It ensures that every user is correctly mapped to a billing account and has the appropriate permissions.

**Core Principle:** Centralized identity management with decentralized platform access.

---

## 2. Core Components

### 2.1 AuthenticationService

The orchestrator for the OAuth registration and login flow.

- **Registration:** Implements the "Master Account First" paradigm.
- **Linking:** Handles the logic for connecting Google OAuth to existing platform-only users.
- **Metadata:** Maintains up-to-date user profile information from the OIDC provider.

### 2.2 IAMService

The central decision point for authorization (see [OAuth Multi-Tenant](../oauth_multi_tenant/README.md)).

- **Replaces:** The legacy `IdentityResolver`.
- **Responsibility:** Determines if a platform user (Slack/Telegram) is authorized to access the system.

### 2.3 InviteCodeService

Manages the creation and consumption of invite codes.

- **Invite Types:** `PERSONAL`, `FAMILY`, `ORGANIZATION`.
- **Auto-Consumption:** Invite codes can be automatically consumed during the OAuth callback flow.
- **Validation:** Ensures codes are not expired or already used.

### 2.4 WhitelistRepository

Controls who can create new accounts in the system.

- **Mechanism:** Email-based whitelist.
- **Enforcement:** Checked by `IAMService` during the first-time registration flow.

---

## 3. Onboarding Flow

### 3.1 Step 1: Web Registration

Users must register via the Web UI using Google OAuth.

1. User logs in at `/auth/login`.
2. `AuthenticationService` handles the callback.
3. If the email is whitelisted, a new `UserProfile` and `BillingAccount` are created.

### 3.2 Step 2: Platform Linking

Once registered, users link their messaging accounts in the User Cabinet.

1. User clicks "Link Slack" or "Link Telegram".
2. The system generates a unique linking token or uses a deep link.
3. The platform adapter calls `user_repo.link_platform_identity()` to bind the IDs.

### 3.3 Step 3: Interaction

After linking, the user can interact with the bot on the chosen platform. The `IAMService` will now recognize the `platform_user_id` and allow access to the user's account.

---

## 4. Data Model

### 4.1 UserProfile

- `user_id`: Internal UUID.
- `external_user_id`: OAuth provider ID.
- `platform_identities`: Map of platform names to IDs (e.g., `{"slack": "U123"}`).
- `account_id`: Reference to the `BillingAccount`.

### 4.2 InviteCode

- `code`: Unique alphanumeric string.
- `type`: The scope of the invite.
- `created_by`: The user who generated the invite.
- `used_by`: The user who consumed the invite.

---

## 5. Code References

- `src/services/authentication_service.py`: Registration and linking logic.
- `src/services/iam_service.py`: Centralized authorization.
- `src/services/invite_code_service.py`: Invite management.
- `src/domain/user.py`: User entity definitions.
- `src/domain/invite_code.py`: Invite entity definitions.

---

## 6. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Self-Service Invites:** Allow account owners to generate and manage invites directly from the Web UI.
- **Social Graph:** Visualize relationships between users and accounts for organizational management.
- **Multi-Email Support:** Allow users to link multiple email addresses to a single profile.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.10
