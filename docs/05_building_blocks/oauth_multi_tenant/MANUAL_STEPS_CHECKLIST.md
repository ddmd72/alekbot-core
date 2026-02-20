# OAuth Multi-Tenant Manual Steps Checklist

**Status:** ✅ All code complete (10/10 sessions)
**Branch:** `multi-tenant`
**Next:** Manual testing and Firebase setup

---

## ✅ What Is Already DONE (automatically)

- [x] Domain models (UserProfile, BillingAccount, Fact) with OAuth fields
- [x] Ports & Interfaces (AuthPort, IAMPort, Repository updates)
- [x] Firebase Auth Adapter (OAuth 2.0 / OIDC)
- [x] OAuth Service & Web Endpoints (5 endpoints + link endpoint)
- [x] IAM Implementation (role-based access control)
- [x] Configuration Inheritance (account defaults + user overrides)
- [x] Repository OAuth Methods (get_by_external_id, link_platform_identity)
- [x] Migration Script (migrate_to_oauth.py)
- [x] Integration Tests (15 tests, 586 lines)
- [x] main.py Integration (OAuth Web App on port 5000)
- [x] Dev Deployment Configuration (dynamic collections)
- [x] Testing Guide & Documentation

**Total:** 4,515 lines of code (2,838 production + 1,289 tests + 388 docs)

---

## 🚦 Steps That MUST Be Performed Manually

### Step 1: Firebase Project Setup (5 minutes)

1. **Create a Firebase project:**
   ```
   https://console.firebase.google.com/
   ```

2. **Enable Authentication:**
   - In Firebase Console → Authentication → Sign-in method
   - Enable "Google" provider
   - Click "Save"

3. **Add OAuth Redirect URI:**
   - In Google Sign-In settings → Add domain:
     - `localhost` (for local testing)
     - `your-cloud-run-domain` (for dev deployment)
   - Authorized redirect URIs:
     - `http://localhost:5000/auth/callback`
     - `https://your-cloud-run-domain/auth/callback`

4. **Get Credentials:**
   - Project Settings → General → Web API Key (copy)
   - Project Settings → Service Accounts → Generate new private key (download JSON)

**Result:** You now have:
- ✅ `FIREBASE_PROJECT_ID`
- ✅ `FIREBASE_WEB_API_KEY`
- ✅ `service-account.json` file

---

### Step 2: Environment Configuration (2 minutes)

Create/update the `.env` file:

```bash
# Copy and replace with your values:
export USE_OAUTH_COLLECTIONS=true
export APP_ENV=development

# Firebase (from Step 1)
export FIREBASE_PROJECT_ID=your-project-id-here
export FIREBASE_WEB_API_KEY=your-web-api-key-here
export GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json

# OAuth Configuration
export OAUTH_REDIRECT_URI=http://localhost:5000/auth/callback
export OAUTH_SESSION_SECRET=$(openssl rand -base64 32)

# Slack (optional, for bot testing)
export SLACK_MODE=socket
export SLACK_BOT_TOKEN=xoxb-your-token
export SLACK_APP_TOKEN=xapp-your-token
```

**Verification:**
```bash
source .env
echo $FIREBASE_PROJECT_ID  # Should show your project ID
```

---

### Step 3: Firestore Backup (REQUIRED!) (2 minutes)

```bash
# Create backup BEFORE migration
gcloud firestore export gs://alek-core-backups/pre-oauth-$(date +%Y%m%d-%H%M%S) \
  --project=YOUR_PROJECT_ID

# Verify the backup was created
gsutil ls -l gs://alek-core-backups/ | tail -3
```

**Result:** Backup in Cloud Storage (for rollback if something goes wrong)

---

### Step 4: Run Migration Script (3 minutes)

```bash
# FIRST run dry-run (no data is written)
python scripts/migrate_to_oauth.py

# Check output:
# ✅ Users migrated: XX
# ✅ Accounts created: XX
# ✅ Facts migrated: XX
# ✅ Errors: 0

# If everything is OK, run live migration
python scripts/migrate_to_oauth.py --live
```

**Result:** New collections have been created:
- ✅ `dev_users_oauth`
- ✅ `dev_accounts_oauth`
- ✅ `dev_facts_oauth`

**Verification:**
```python
from google.cloud import firestore

db = firestore.Client()

users = list(db.collection("dev_users_oauth").limit(3).stream())
print(f"✅ OAuth users: {len(users)}")

accounts = list(db.collection("dev_accounts_oauth").limit(3).stream())
print(f"✅ OAuth accounts: {len(accounts)}")
```

---

### Step 5: Start Application (1 minute)

```bash
# Load environment
source .env

# Start bot + OAuth Web App
make dev
```

**Check the logs:**
```
🔐 Loading configuration...
💳 Initializing Account Repository...
📂 User Repository initialized. Collection: dev_users_oauth  ← OAuth collections!
🔐 Initializing OAuth Web App...
🌐 Starting OAuth Web App on http://0.0.0.0:5000  ← OAuth app started!
✅ OAuth Web App started on port 5000
🚀 Starting Slack Adapter in Socket Mode...
```

**If you see these lines → ✅ Everything is working!**

---

### Step 6: Test OAuth Registration (3 minutes)

1. **Open a browser:**
   ```bash
   open http://localhost:5000/auth/login
   ```

2. **Complete the OAuth flow:**
   - Choose a Google account
   - Grant permissions
   - Redirected to `/auth/callback`

3. **Check the JSON response:**
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

4. **Check in Firestore:**
   ```python
   from google.cloud import firestore

   db = firestore.Client()

   # Find the most recently created user
   users = list(db.collection("dev_users_oauth").order_by("created_at", direction="DESCENDING").limit(1).stream())
   user = users[0].to_dict()

   print(f"✅ User: {user['user_id']}")
   print(f"✅ external_user_id: {user['external_user_id']}")  # firebase|abc123...
   print(f"✅ account_id: {user['account_id']}")

   # Check account
   account = db.collection("dev_accounts_oauth").document(user['account_id']).get().to_dict()
   print(f"✅ IAM policy: {account['iam_policy']}")  # {user_id: "owner"}
   ```

**Result:** ✅ New user created via OAuth!

---

### Step 7: Link OAuth to U_DMYTRO_CORE (5 minutes)

**Method A: Manual migration (simplest)**

1. Register a test account (already done in Step 6)

2. Get the `external_user_id` from the new user:
   ```python
   from google.cloud import firestore

   db = firestore.Client()

   # Most recently created user
   users = list(db.collection("dev_users_oauth").order_by("created_at", direction="DESCENDING").limit(1).stream())
   new_user = users[0].to_dict()

   external_user_id = new_user['external_user_id']
   print(f"🔑 external_user_id: {external_user_id}")
   ```

3. Copy into U_DMYTRO_CORE:
   ```python
   # Get existing user
   dmytro_doc = db.collection("dev_users_oauth").document("U_DMYTRO_CORE").get()

   if not dmytro_doc.exists:
       print("❌ U_DMYTRO_CORE not found in dev_users_oauth")
       print("💡 Run migration first: python scripts/migrate_to_oauth.py --live")
   else:
       dmytro = dmytro_doc.to_dict()

       # Link OAuth identity
       dmytro['external_user_id'] = external_user_id
       dmytro['auth_metadata'] = new_user['auth_metadata']

       # Save
       db.collection("dev_users_oauth").document("U_DMYTRO_CORE").set(dmytro)

       print("✅ Google OAuth linked to U_DMYTRO_CORE")
       print(f"✅ external_user_id: {external_user_id}")
   ```

4. Delete the temporary test user:
   ```python
   # Cleanup test user
   db.collection("dev_users_oauth").document(new_user['user_id']).delete()
   db.collection("dev_accounts_oauth").document(new_user['account_id']).delete()
   print("✅ Test user deleted")
   ```

5. Verify that you can now log in as U_DMYTRO_CORE:
   ```bash
   open http://localhost:5000/auth/login
   # Should log in as U_DMYTRO_CORE with your email
   ```

**Result:** ✅ U_DMYTRO_CORE is now linked to Google OAuth!

---

### Step 8: Verify All Features (5 minutes)

Verify that everything works:

```python
from google.cloud import firestore
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_iam_adapter import FirestoreIAMAdapter
from src.services.configuration_service import ConfigurationService
from src.config.environment import EnvironmentConfig
from src.ports.iam_port import Action, ResourceType

# Initialize
db = firestore.AsyncClient()
env_config = EnvironmentConfig()
account_repo = FirestoreAccountRepository(db, env_config.account_collection_name)
user_repo = FirestoreUserRepository(db, env_config, account_repo)
iam = FirestoreIAMAdapter(account_repo)
config_service = ConfigurationService()

# Get your user
user = await user_repo.get_user("U_DMYTRO_CORE")
account = await account_repo.get_account(user.account_id)

# Test 1: OAuth identity
print(f"✅ User: {user.user_id}")
print(f"✅ external_user_id: {user.external_user_id}")  # Should be firebase|...
print(f"✅ email: {user.email}")

# Test 2: IAM permissions
role = await iam.get_user_role(user.user_id, account.account_id)
print(f"✅ Role: {role}")  # Should be Role.OWNER

can_admin = await iam.can_access_resource(
    user.user_id, ResourceType.ACCOUNT, account.account_id, Action.ADMIN, account.account_id
)
print(f"✅ Can admin account: {can_admin}")  # Should be True

# Test 3: Configuration inheritance
effective_config = config_service.get_effective_config(user, account)
print(f"✅ Temperature: {effective_config.temperature}")
print(f"✅ Agent tiers: {effective_config.agent_tiers}")
```

**All tests passed → ✅ OAuth Multi-Tenant is ready to use!**

---

## 📊 Checklist Summary

- [ ] Firebase project created, Google Sign-In enabled
- [ ] Environment variables configured in `.env`
- [ ] Firestore backup created
- [ ] Migration script executed (`--live`)
- [ ] Application started (`make dev`)
- [ ] OAuth registration tested (new user created)
- [ ] OAuth linked to U_DMYTRO_CORE
- [ ] All features verified (OAuth, IAM, Config)

**When all checkboxes are ✅ → ready to deploy to Cloud Run!**

---

## 🚀 Next: Deploy to Cloud Run Dev

See [DEV_DEPLOYMENT.md](DEV_DEPLOYMENT.md) for deployment instructions.

---

**Last Updated:** 2026-01-31
**Status:** Ready for manual testing
**Branch:** `multi-tenant`
