# OAuth Multi-Tenant Dev Deployment Guide

**Goal:** Deploy the `multi-tenant` branch to the dev environment and work with OAuth collections.

---

## Current State

- ✅ All changes are in the `multi-tenant` branch
- ✅ Dev Firestore environment exists (`dev_users`, `dev_accounts`, `dev_facts`)
- ✅ Migration script is ready to create OAuth collections
- ✅ Code updated to support OAuth collections via environment variable

---

## Step 1: Create a Dev Firestore Backup (REQUIRED!)

```bash
# Create a backup (replace PROJECT_ID with your GCP project)
gcloud firestore export gs://alek-core-backups/dev-pre-oauth-$(date +%Y%m%d-%H%M%S) \
  --project=PROJECT_ID

# Verify that the backup was created
gsutil ls -l gs://alek-core-backups/ | tail -5
```

---

## Step 2: Run the Data Migration in Dev

### 2.1 Dry-run (FIRST!)

```bash
# Check without writing data
python scripts/migrate_to_oauth.py

# Check output:
# - Users migrated: XX
# - Accounts created: XX
# - Facts migrated: XX
# - Errors: 0 (must be 0!)
```

### 2.2 Live Migration

```bash
# ONLY after a successful dry-run!
python scripts/migrate_to_oauth.py --live

# Result: will create collections
# - dev_users_oauth
# - dev_accounts_oauth
# - dev_facts_oauth
```

### 2.3 Verify the Migration

```python
from google.cloud import firestore

db = firestore.Client()

# Check users
users = list(db.collection("dev_users_oauth").limit(5).stream())
print(f"✅ OAuth users: {len(users)}")

# Check the first user
user = users[0].to_dict()
print(f"Has external_user_id: {'external_user_id' in user}")
print(f"Has account_id: {'account_id' in user}")
print(f"Has platform_identities: {'platform_identities' in user}")

# Check accounts
accounts = list(db.collection("dev_accounts_oauth").limit(5).stream())
print(f"✅ OAuth accounts: {len(accounts)}")

# Check the first account
account = accounts[0].to_dict()
print(f"Has iam_policy: {'iam_policy' in account}")
print(f"IAM policy: {account.get('iam_policy')}")

# Check facts
facts = list(db.collection("dev_facts_oauth").limit(5).stream())
print(f"✅ OAuth facts: {len(facts)}")

# Check the first fact
fact = facts[0].to_dict()
print(f"Has account_id: {'account_id' in fact}")
print(f"Has created_by_user_id: {'created_by_user_id' in fact}")
print(f"Has visibility: {'visibility' in fact}")
```

---

## Step 3: Configure the Environment for OAuth Collections

### 3.1 Local Development

Add to `.env` or export:

```bash
# Enable OAuth collections
export USE_OAUTH_COLLECTIONS=true

# Dev environment
export APP_ENV=development

# Firebase configuration (for OAuth)
export FIREBASE_PROJECT_ID=your-project-id
export FIREBASE_WEB_API_KEY=your-web-api-key
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# OAuth configuration
export OAUTH_REDIRECT_URI=http://localhost:5000/auth/callback
export OAUTH_SESSION_SECRET=your-32-char-secret-minimum-here

# Slack (if you use it)
export SLACK_MODE=socket
```

### 3.2 Cloud Run / GCE Deployment

**In Cloud Run:**
1. Go to Cloud Console → Cloud Run → Service
2. Edit & Deploy New Revision
3. Variables & Secrets → Add Variable:
   - `USE_OAUTH_COLLECTIONS` = `true`
   - `APP_ENV` = `development`
   - `FIREBASE_PROJECT_ID` = `your-project-id`
   - `FIREBASE_WEB_API_KEY` = `your-web-api-key`
   - `OAUTH_REDIRECT_URI` = `https://your-dev-service.run.app/auth/callback`
   - `OAUTH_SESSION_SECRET` = (create in Secret Manager!)

**Docker:**
```dockerfile
ENV USE_OAUTH_COLLECTIONS=true
ENV APP_ENV=development
```

---

## Step 4: Update Application Code (if needed)

### 4.1 Verify Repository Initialization

Find where repositories are created (usually in `main.py` or DI container):

```python
from src.config.environment import EnvironmentConfig

env_config = EnvironmentConfig()

# ✅ CORRECT - uses env_config.user_collection_name
user_repo = FirestoreUserRepository(
    db_client=db,
    env_config=env_config,
    account_repo=account_repo
)

# ✅ CORRECT - pass the full collection name
account_repo = FirestoreAccountRepository(
    db_client=db,
    collection_name=env_config.account_collection_name
)
```

### 4.2 Check for Direct Collection Usage

If there are hardcoded collection names anywhere in the code:

```python
# ❌ WRONG
db.collection("dev_users")

# ✅ CORRECT
db.collection(env_config.user_collection_name)
```

Search:
```bash
# Find hardcoded names
grep -r "dev_users" src/
grep -r "dev_accounts" src/
grep -r "dev_facts" src/
```

---

## Step 5: Run and Test

### 5.1 Local Run

```bash
# Make sure USE_OAUTH_COLLECTIONS=true
export USE_OAUTH_COLLECTIONS=true

# Start the application
python src/main.py
# or
make dev
```

**Check the logs:**
```
📂 User Repository initialized. Collection: dev_users_oauth
📂 Account Repository initialized. Collection: dev_accounts_oauth
```

### 5.2 Testing OAuth

**New user registration:**
1. Open `http://localhost:5000/auth/login`
2. Complete the OAuth flow via Google
3. Verify in Firestore that the following were created:
   - User in `dev_users_oauth` with `external_user_id`
   - Account in `dev_accounts_oauth` with `iam_policy`

**IAM verification:**
```python
from src.adapters.firestore_iam_adapter import FirestoreIAMAdapter
from src.ports.iam_port import Role, ResourceType, Action

iam = FirestoreIAMAdapter(account_repo)

# Verify that the user is OWNER of their account
role = await iam.get_user_role("user-id", "account-id")
print(f"Role: {role}")  # should be Role.OWNER

# Check permissions
can_admin = await iam.can_access_resource(
    "user-id", ResourceType.ACCOUNT, "account-id", Action.ADMIN, "account-id"
)
print(f"Can admin: {can_admin}")  # should be True
```

**Configuration Inheritance verification:**
```python
from src.services.configuration_service import ConfigurationService

config_service = ConfigurationService()

# Get effective config for user
effective = config_service.get_effective_config(user, account)
print(f"Temperature: {effective.temperature}")
print(f"Agent tiers: {effective.agent_tiers}")
```

---

## Step 6: Deploy to Cloud Run (Dev)

### 6.1 Build & Deploy from the multi-tenant branch

```bash
# Switch to the branch
git checkout multi-tenant

# Build Docker image
gcloud builds submit --tag gcr.io/PROJECT_ID/alek-core:dev-oauth

# Deploy to Cloud Run (dev service)
gcloud run deploy alek-core-dev \
  --image gcr.io/PROJECT_ID/alek-core:dev-oauth \
  --platform managed \
  --region us-central1 \
  --set-env-vars USE_OAUTH_COLLECTIONS=true,APP_ENV=development \
  --service-account alek-core-dev@PROJECT_ID.iam.gserviceaccount.com
```

### 6.2 Verify the Deployment

```bash
# Get the URL
gcloud run services describe alek-core-dev --region us-central1 --format='value(status.url)'

# Check logs
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=alek-core-dev" --limit 50
```

---

## Rollback (if something goes wrong)

### Option 1: Switch back to the old collections

```bash
# Remove the environment variable
unset USE_OAUTH_COLLECTIONS
# or
export USE_OAUTH_COLLECTIONS=false

# Restart the application - it will use dev_users, dev_accounts, dev_facts
```

### Option 2: Delete OAuth collections

```python
from google.cloud import firestore

db = firestore.Client()

# Delete OAuth collections
for collection in ["dev_users_oauth", "dev_accounts_oauth", "dev_facts_oauth"]:
    docs = db.collection(collection).stream()
    batch = db.batch()
    count = 0
    for doc in docs:
        batch.delete(doc.reference)
        count += 1
        if count % 500 == 0:
            batch.commit()
            batch = db.batch()
    if count % 500 != 0:
        batch.commit()
    print(f"✅ Deleted {count} documents from {collection}")
```

### Option 3: Restore from backup

```bash
# Find the latest backup
gsutil ls gs://alek-core-backups/ | grep dev-pre-oauth

# Restore
gcloud firestore import gs://alek-core-backups/dev-pre-oauth-YYYYMMDD-HHMMSS
```

---

## Troubleshooting

### "Collection dev_users_oauth not found"

**Problem:** Migration was not run or did not complete.

**Solution:**
```bash
python scripts/migrate_to_oauth.py --live
```

### "User Repository initialized. Collection: dev_users" (without _oauth)

**Problem:** `USE_OAUTH_COLLECTIONS` is not set or is set to false.

**Solution:**
```bash
export USE_OAUTH_COLLECTIONS=true
```

### OAuth callback fails with "Invalid state"

**Problem:** `OAUTH_SESSION_SECRET` is not set or is less than 32 characters.

**Solution:**
```bash
export OAUTH_SESSION_SECRET="$(openssl rand -base64 32)"
```

### IAM checks fail with "Account not found"

**Problem:** User is not linked to an Account, or the migration did not create accounts.

**Solution:**
1. Check `dev_accounts_oauth` in Firestore
2. Verify that user.account_id references an existing account
3. Re-run the migration

---

## Next Steps

After a successful deployment to dev:

1. ✅ Test OAuth registration/login
2. ✅ Test IAM permissions (OWNER, MEMBER, VIEWER)
3. ✅ Test configuration inheritance
4. ✅ Test platform linking (Slack, Telegram)
5. ✅ Monitor errors in logs
6. ✅ After 1-2 weeks of stability → merge into main

---

**Last Updated:** 2026-01-31
**Status:** Ready for dev deployment
**Branch:** `multi-tenant`
