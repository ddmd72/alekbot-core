# OAuth Multi-Tenant Testing Guide

**Goal:** Test OAuth registration, login, and linking for existing users.

---

## Prerequisites

### 1. Firebase Project Setup

If not yet configured, create a Firebase project:

```bash
# 1. Create Firebase project in console: https://console.firebase.google.com/
# 2. Enable Authentication → Google Sign-In
# 3. Add OAuth redirect URI: http://localhost:5000/auth/callback
```

### 2. Environment Variables

Create a `.env` file with credentials:

```bash
# OAuth Collections
export USE_OAUTH_COLLECTIONS=true
export APP_ENV=development

# Firebase Configuration
export FIREBASE_PROJECT_ID=your-project-id
export FIREBASE_WEB_API_KEY=your-web-api-key
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# OAuth Configuration
export OAUTH_REDIRECT_URI=http://localhost:5000/auth/callback
export OAUTH_SESSION_SECRET=$(openssl rand -base64 32)

# Slack (optional for testing)
export SLACK_MODE=socket
export SLACK_BOT_TOKEN=xoxb-your-token
export SLACK_APP_TOKEN=xapp-your-token
```

### 3. Run Migration (First Time Only)

Create OAuth collections:

```bash
# Dry-run first
python scripts/migrate_to_oauth.py

# If looks good, run live
python scripts/migrate_to_oauth.py --live
```

Verify collections created:
```bash
# Check in Firebase Console or run:
from google.cloud import firestore
db = firestore.Client()

users = list(db.collection("dev_users_oauth").limit(1).stream())
accounts = list(db.collection("dev_accounts_oauth").limit(1).stream())

print(f"✅ OAuth collections exist: users={len(users)}, accounts={len(accounts)}")
```

---

## Test Scenario 1: New User Registration via Google OAuth

### Step 1: Start Application

```bash
# Load environment variables
source .env

# Start bot + OAuth Web App
make dev
```

Check logs:
```
🔐 Loading configuration...
💳 Initializing Account Repository...
📂 User Repository initialized. Collection: dev_users_oauth
🔐 Initializing OAuth Web App...
🌐 Starting OAuth Web App on http://0.0.0.0:5000
✅ OAuth Web App started on port 5000
🚀 Starting Slack Adapter in Socket Mode...
```

### Step 2: Open Browser

Navigate to: `http://localhost:5000/auth/login`

**Expected Flow:**
1. Browser redirects to Google OAuth consent screen
2. Select Google account
3. Grant permissions
4. Redirected back to `http://localhost:5000/auth/callback`
5. See JSON response:
```json
{
  "success": true,
  "user": {
    "user_id": "uuid-here",
    "email": "your-email@gmail.com",
    "display_name": "Your Name"
  },
  "account": {
    "account_id": "uuid-here",
    "tier": "free"
  },
  "access_token": "jwt-token-here"
}
```

### Step 3: Verify in Firestore

```python
from google.cloud import firestore

db = firestore.Client()

# Get latest user
users = list(db.collection("dev_users_oauth").order_by("created_at", direction="DESCENDING").limit(1).stream())
user = users[0].to_dict()

print("✅ User created:")
print(f"  user_id: {user['user_id']}")
print(f"  external_user_id: {user['external_user_id']}")  # Should be "firebase|abc123..."
print(f"  email: {user['email']}")
print(f"  account_id: {user['account_id']}")

# Get account
account_id = user['account_id']
account = db.collection("dev_accounts_oauth").document(account_id).get().to_dict()

print("✅ Account created:")
print(f"  account_id: {account['account_id']}")
print(f"  tier: {account['tier']}")
print(f"  iam_policy: {account['iam_policy']}")  # Should have user as OWNER
```

### Step 4: Test /auth/me Endpoint

```bash
# Extract access_token from callback response
ACCESS_TOKEN="paste-jwt-token-here"

# Test /auth/me
curl -X GET http://localhost:5000/auth/me \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Expected response:
```json
{
  "user": {
    "user_id": "uuid",
    "email": "your-email@gmail.com",
    "external_user_id": "firebase|abc123..."
  },
  "account": {
    "account_id": "uuid",
    "tier": "free",
    "role": "owner"
  }
}
```

---

## Test Scenario 2: Link Google OAuth to Existing User

**Use Case:** You already have a user (e.g. YOUR_USER_ID) and want to link Google OAuth to it.

### Option A: Via API (for testing)

```bash
# 1. Get existing user_id (e.g. via Slack bot or Firestore)
USER_ID="your-existing-user-id"

# 2. Login via Slack/Telegram to get access_token (or create a mock token)
# TODO: This requires Slack authentication first - see Option B for a simpler approach

# 3. Start OAuth flow in browser
open http://localhost:5000/auth/login

# 4. After Google redirects back, extract authorization code from URL
# URL will be: http://localhost:5000/auth/callback?code=AUTHORIZATION_CODE&state=STATE

# 5. Call /auth/link-oauth with code
curl -X POST http://localhost:5000/auth/link-oauth \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "code": "AUTHORIZATION_CODE",
    "state": "STATE"
  }'
```

Expected response:
```json
{
  "success": true,
  "message": "Google OAuth linked successfully",
  "user": {
    "user_id": "your-existing-user-id",
    "external_user_id": "firebase|abc123...",
    "email": "your-email@gmail.com"
  }
}
```

### Option B: Manual migration (for YOUR_USER_ID)

**Simplest way to link OAuth:**

1. **Register a new test account via OAuth:**
   ```bash
   open http://localhost:5000/auth/login
   ```

2. **Get external_user_id of the new user:**
   ```python
   from google.cloud import firestore

   db = firestore.Client()
   users = list(db.collection("dev_users_oauth").order_by("created_at", direction="DESCENDING").limit(1).stream())
   new_user = users[0].to_dict()

   external_user_id = new_user['external_user_id']
   print(f"external_user_id: {external_user_id}")
   ```

3. **Copy external_user_id to YOUR_USER_ID:**
   ```python
   # Get your existing user
   dmytro = db.collection("dev_users_oauth").document("YOUR_USER_ID").get()
   dmytro_data = dmytro.to_dict()

   # Update with OAuth identity
   dmytro_data['external_user_id'] = external_user_id  # From step 2
   dmytro_data['auth_metadata'] = new_user['auth_metadata']

   # Save
   db.collection("dev_users_oauth").document("YOUR_USER_ID").set(dmytro_data)

   print("✅ OAuth linked to YOUR_USER_ID")
   ```

4. **Delete the temporary user (optional):**
   ```python
   # Delete test user created in step 1
   db.collection("dev_users_oauth").document(new_user['user_id']).delete()
   db.collection("dev_accounts_oauth").document(new_user['account_id']).delete()
   ```

5. **Verify you can now log in via OAuth:**
   ```bash
   open http://localhost:5000/auth/login
   # Should log in as YOUR_USER_ID
   ```

---

## Test Scenario 3: IAM Permissions

Test that OWNER role works correctly:

```python
from src.adapters.firestore_iam_adapter import FirestoreIAMAdapter
from src.ports.iam_port import Action, ResourceType

# Initialize IAM adapter
iam = FirestoreIAMAdapter(account_repo)

# Get your user and account
user = await user_repo.get_user("your-user-id")
account = await account_repo.get_account(user.account_id)

# Test OWNER permissions
can_admin = await iam.can_access_resource(
    user.user_id,
    ResourceType.ACCOUNT,
    account.account_id,
    Action.ADMIN,
    account.account_id
)
print(f"Can admin account: {can_admin}")  # Should be True

# Test role retrieval
role = await iam.get_user_role(user.user_id, account.account_id)
print(f"User role: {role}")  # Should be Role.OWNER
```

---

## Test Scenario 4: Configuration Inheritance

Test that account defaults + user overrides work:

```python
from src.services.configuration_service import ConfigurationService

config_service = ConfigurationService()

# Get effective config (account defaults + user overrides)
effective_config = config_service.get_effective_config(user, account)

print(f"Temperature: {effective_config.temperature}")  # Should use account default
print(f"Agent tiers: {effective_config.agent_tiers}")

# Check if user has overrides
has_overrides = config_service.has_user_overrides(user)
print(f"Has user overrides: {has_overrides}")

# Get override summary
if has_overrides:
    summary = config_service.get_override_summary(user)
    print(f"Overrides: {summary}")
```

---

## Troubleshooting

### "OAuth Web App initialization failed"

**Problem:** Firebase credentials not configured.

**Solution:**
```bash
# Check environment variables
echo $FIREBASE_PROJECT_ID
echo $FIREBASE_WEB_API_KEY
echo $GOOGLE_APPLICATION_CREDENTIALS

# Verify service account file exists
ls -la $GOOGLE_APPLICATION_CREDENTIALS
```

### "Invalid state parameter"

**Problem:** CSRF state cookie expired or not set.

**Solution:**
- Clear browser cookies for localhost:5000
- Try OAuth flow again from `/auth/login`

### "Collection dev_users_oauth not found"

**Problem:** Migration not run.

**Solution:**
```bash
python scripts/migrate_to_oauth.py --live
```

### "OAuth identity already linked to another user"

**Problem:** Google account already registered.

**Solution:**
- Use different Google account
- Or delete existing user and re-register
- Or use `/auth/link-oauth` to link to existing user

---

## Next Steps

After successful testing:

1. ✅ OAuth registration works → can register new users
2. ✅ OAuth linking works → can link OAuth to Slack/Telegram users
3. ✅ IAM permissions work → OWNER can manage the account
4. ✅ Configuration inheritance → 99% of users use account defaults

**Ready for dev deployment:**
- See [DEV_DEPLOYMENT.md](DEV_DEPLOYMENT.md) for Cloud Run deployment instructions

---

**Last Updated:** 2026-01-31
**Status:** Ready for testing
**Branch:** `multi-tenant`
