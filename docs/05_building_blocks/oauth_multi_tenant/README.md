# OAuth Multi-Tenant (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the multi-tenant architecture, OAuth authentication flows, and identity management system.

### When to Read

- **For AI Agents:** Before modifying authentication logic, IAM policies, or user/account repositories.
- **For Developers:** When troubleshooting login issues, platform linking, or data isolation bugs.

### When to Update

This document MUST be updated when:

- [ ] The OAuth provider (Firebase/Google) or OIDC flow changes.
- [ ] The IAM role model (OWNER, MEMBER, VIEWER) is modified.
- [ ] The "Master Account First" registration logic changes.
- [ ] New platform identity linking logic is introduced.
- [ ] Storage schema for users or accounts is restructured.

### Cross-References

- **Context:** [../../03_context/README.md](../../03_context/README.md)
- **User Management Guide:** [../../08_concepts/user_management_complete_guide.md](../../08_concepts/user_management_complete_guide.md)
- **Security Validation:** [../security_validation/README.md](../security_validation/README.md)

---

## 1. Overview

Alek-Core is a **Multi-Tenant Bot-as-a-Service** platform. It supports multiple isolated accounts (tenants), each containing one or more users. Authentication is handled via Google OAuth 2.0, and authorization is centralized in the `IAMService`.

**Core Principle:** Data isolation is enforced at the account level. Users belong to accounts, and facts/sessions are owned by accounts.

---

## 2. Multi-Tenancy Model

### 2.1 Master Account First Paradigm

Every new user registration automatically creates a new `BillingAccount`.

- **Owner:** The registering user is assigned the `OWNER` role.
- **Isolation:** All data created by this user is scoped to their new `account_id`.
- **Collaboration:** Owners can invite other users to their account (Phase 2+).

### 2.2 Identity Model

- **UserProfile:** Internal system identity (UUID).
- **External Identity:** OAuth-provided ID (e.g., `firebase|abc123`).
- **Platform Identities:** Bindings to messaging platforms (e.g., `slack:U123`, `telegram:456`).
- **Linking:** A single `UserProfile` can link multiple platform identities to one OAuth account.

---

## 3. Authentication Flow (Web UI)

The Web UI is the only entry point for account creation and management.

1. **Login:** User initiates OAuth flow at `/auth/login`.
2. **Callback:** Backend receives OIDC tokens at `/auth/callback`.
3. **Resolution:**
   - If `external_user_id` exists: Authenticate existing user.
   - If `email` exists in system: Link OAuth to existing platform user.
   - If new user: Check `WhitelistRepository`. If allowed, create new `UserProfile` + `BillingAccount`.
4. **Session:** Issue JWT access and refresh tokens stored in secure, HttpOnly cookies.

---

## 4. IAM & Authorization

### 4.1 IAMService

The central authority for all access decisions. Every request from Slack, Telegram, or Web API must pass through `iam_service.authorize()`.

**Decision Logic:**

- **Branch 1 (Chat):** Does `platform_user_id` exist? If yes, allow access to the linked account.
- **Branch 2 (Web):** Does `external_user_id` exist? If yes, allow access to the User Cabinet.
- **Branch 3 (New):** Is the email whitelisted? If yes, trigger account creation.

### 4.2 Roles & Permissions

- **OWNER:** Full access to account settings, billing, and all facts.
- **MEMBER:** Can interact with the bot and see shared facts.
- **VIEWER:** Read-only access to shared facts (no bot interaction).

---

## 5. Platform Linking

Users can unify their identities across platforms:

1. **Step 1:** Register via Web UI (Google OAuth).
2. **Step 2:** Open User Cabinet.
3. **Step 3:** Click "Link Slack" or "Link Telegram".
4. **Step 4:** The system binds the platform ID to the `UserProfile`, enabling persistent memory across all clients.

---

## 6. Code References

- `src/domain/user.py`: `UserProfile` and `UserBotConfig` definitions.
- `src/domain/billing.py`: `BillingAccount` and `IAMPolicy` definitions.
- `src/services/authentication_service.py`: OAuth flow orchestration.
- `src/services/iam_service.py`: Centralized authorization logic.
- `src/web/oauth_app.py`: Web endpoints for authentication.

---

## 7. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Invite System:** Full implementation of organization-level invite codes.
- **RBAC:** Granular permission control for enterprise accounts.
- **Multi-Provider:** Support for AWS Cognito and Okta alongside Firebase.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.4
