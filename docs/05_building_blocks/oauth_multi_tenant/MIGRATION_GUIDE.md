# OAuth Multi-Tenant Data Migration Guide

**Session 8: Data Migration Script**
**RFC:** [MULTI_TENANT_OAUTH_RFC.md](../../10_rfcs/MULTI_TENANT_OAUTH_RFC.md)
**Migration Script:** [scripts/migrate_to_oauth.py](../../../scripts/migrate_to_oauth.py)

---

## Overview

This guide explains how to migrate existing Alek-Core data from single-user schema to OAuth multi-tenant schema.

**Migration Strategy:** New collections with `_oauth` suffix
- `dev_users` → `dev_users_oauth`
- `dev_accounts` → `dev_accounts_oauth`
- `dev_facts` → `dev_facts_oauth`

**Safety:** Old collections remain untouched for rollback capability.

---

## Data Transformations

### 1. Users (`dev_users` → `dev_users_oauth`)

**New Fields Added:**
- `external_user_id`: `None` (will be set during OAuth registration)
- `auth_metadata`: `None` (OAuth provider metadata)
- `platform_identities`: `{}` (Slack, Telegram IDs)
- `account_id`: Link to new BillingAccount

**Existing Fields:** Preserved as-is

**Action:** For each user, creates a default BillingAccount with user as OWNER.

### 2. Accounts (`dev_accounts` → `dev_accounts_oauth`)

**New Accounts Created:** One account per user

**Account Structure:**
```python
{
    "account_id": "account-{user_id}",
    "tier": "free",
    "usage": {...},  # Reset to zero
    "iam_policy": {
        "{user_id}": "owner"  # User owns their account
    },
    "account_defaults": None,  # No shared config yet
    "daily_token_limit": 100_000,
    "monthly_cost_limit": 50.0,
    "is_active": True
}
```

### 3. Facts (`dev_facts` → `dev_facts_oauth`)

**Field Transformations:**
- `owner_id` → `created_by_user_id` (renamed)
- `account_id`: Added (lookup from user's account)
- `visibility`: Added (default: `"account_shared"`)

**Existing Fields:** Preserved as-is

**Visibility Logic:**
- All migrated facts default to `ACCOUNT_SHARED`
- Users can change to `USER_PRIVATE` after migration if needed

---

## Prerequisites

### 1. Firestore Backup

**CRITICAL:** Create backup before running migration.

```bash
# Backup to GCS bucket
gcloud firestore export gs://alek-core-backups/pre-oauth-migration-$(date +%Y%m%d-%H%M%S)
```

**Verify backup:**
```bash
# List recent backups
gsutil ls -l gs://alek-core-backups/ | tail -5
```

### 2. Verify Current Data

Check source collections have data:
```python
from google.cloud import firestore

db = firestore.Client()

# Check users
users = list(db.collection("dev_users").limit(5).stream())
print(f"Users found: {len(users)}")

# Check facts
facts = list(db.collection("dev_facts").limit(5).stream())
print(f"Facts found: {len(facts)}")
```

### 3. Verify Target Collections Empty

Ensure `_oauth` collections don't exist or are empty:
```python
# Check target collections
oauth_users = list(db.collection("dev_users_oauth").limit(1).stream())
if oauth_users:
    print("❌ Target collection dev_users_oauth already has data!")
else:
    print("✅ Target collection dev_users_oauth is empty")
```

---

## Running Migration

### Step 1: Dry-Run (Recommended)

**Always run dry-run first** to verify transformations without writing data.

```bash
# From project root
python scripts/migrate_to_oauth.py

# Or explicitly
python scripts/migrate_to_oauth.py --dry-run
```

**Expected Output:**
```
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] OAuth Multi-Tenant Data Migration (Session 8)
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] Mode: DRY-RUN (no data written)
🔵 [DRY-RUN] Source prefix: dev_
🔵 [DRY-RUN] Target prefix: dev_
🔵 [DRY-RUN]
🔵 [DRY-RUN] Verifying migration prerequisites...
🔵 [DRY-RUN] ✅ Source collection dev_users exists
🔵 [DRY-RUN] ✅ Target collection dev_users_oauth is empty
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] PHASE 1: Migrating Users and Creating Accounts
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] Migrated 10 users...
🔵 [DRY-RUN] ✅ Users migrated: 15
🔵 [DRY-RUN] ✅ Accounts created: 15
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] PHASE 2: Migrating Facts
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] Migrated 100 facts...
🔵 [DRY-RUN] ✅ Facts migrated: 234
🔵 [DRY-RUN]
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] MIGRATION SUMMARY
🔵 [DRY-RUN] ================================================================================
🔵 [DRY-RUN] Duration: 0:00:12.345678
🔵 [DRY-RUN] Users migrated: 15
🔵 [DRY-RUN] Accounts created: 15
🔵 [DRY-RUN] Facts migrated: 234
🔵 [DRY-RUN] Errors: 0
🔵 [DRY-RUN]
🔵 [DRY-RUN] ✅ DRY-RUN COMPLETE - No data was written
🔵 [DRY-RUN] To perform actual migration, run with --live flag
```

**Review Output:**
- ✅ Check user/fact counts match expectations
- ✅ Verify no critical errors
- ✅ Review any warnings

### Step 2: Live Migration

**Only after successful dry-run:**

```bash
python scripts/migrate_to_oauth.py --live
```

**Expected Output:**
```
🟢 [LIVE] ================================================================================
🟢 [LIVE] OAuth Multi-Tenant Data Migration (Session 8)
🟢 [LIVE] ================================================================================
🟢 [LIVE] Mode: LIVE (data will be written)
...
🟢 [LIVE] ✅ MIGRATION COMPLETE
🟢 [LIVE] Old collections remain untouched: dev_users, dev_accounts, dev_facts
🟢 [LIVE] New collections created: dev_users_oauth, dev_accounts_oauth, dev_facts_oauth
```

### Step 3: Verify Migration

Check new collections have correct data:

```python
from google.cloud import firestore

db = firestore.Client()

# Verify users
oauth_users = list(db.collection("dev_users_oauth").limit(5).stream())
print(f"✅ OAuth users: {len(oauth_users)}")

# Check first user has OAuth fields
user = oauth_users[0].to_dict()
assert "external_user_id" in user
assert "account_id" in user
assert "platform_identities" in user
print(f"✅ User {oauth_users[0].id} has OAuth fields")

# Verify accounts
oauth_accounts = list(db.collection("dev_accounts_oauth").limit(5).stream())
print(f"✅ OAuth accounts: {len(oauth_accounts)}")

# Check first account has IAM policy
account = oauth_accounts[0].to_dict()
assert "iam_policy" in account
assert len(account["iam_policy"]) > 0
print(f"✅ Account {oauth_accounts[0].id} has IAM policy")

# Verify facts
oauth_facts = list(db.collection("dev_facts_oauth").limit(5).stream())
print(f"✅ OAuth facts: {len(oauth_facts)}")

# Check first fact has new ownership fields
fact = oauth_facts[0].to_dict()
assert "account_id" in fact
assert "created_by_user_id" in fact
assert "visibility" in fact
print(f"✅ Fact {oauth_facts[0].id} has new ownership fields")
```

---

## Advanced Options

### Custom Collection Prefixes

Migrate from/to different environments:

```bash
# Migrate from prod to staging
python scripts/migrate_to_oauth.py \
  --source-prefix prod_ \
  --target-prefix staging_ \
  --live
```

---

## Rollback Procedure

If migration fails or issues discovered:

### Option 1: Delete OAuth Collections

```python
from google.cloud import firestore

db = firestore.Client()

# Delete all documents in oauth collections
collections = ["dev_users_oauth", "dev_accounts_oauth", "dev_facts_oauth"]

for collection_name in collections:
    collection_ref = db.collection(collection_name)
    docs = collection_ref.stream()

    batch = db.batch()
    count = 0

    for doc in docs:
        batch.delete(doc.reference)
        count += 1

        if count % 500 == 0:  # Commit every 500 deletes
            batch.commit()
            batch = db.batch()

    if count % 500 != 0:  # Commit remaining
        batch.commit()

    print(f"✅ Deleted {count} documents from {collection_name}")
```

### Option 2: Restore from Backup

```bash
# List backups
gcloud firestore operations list

# Restore from backup
gcloud firestore import gs://alek-core-backups/[BACKUP_FOLDER]
```

---

## Troubleshooting

### Migration Fails: "Target collection already has data"

**Cause:** Target `_oauth` collections not empty.

**Solution:**
1. Review existing data in `_oauth` collections
2. Delete if safe (see Rollback Procedure)
3. Re-run migration

### Migration Shows Warnings: "Cannot find account for user X"

**Cause:** Fact references non-existent user (orphaned fact).

**Solution:**
- Review orphaned facts
- Option 1: Delete orphaned facts from source
- Option 2: Create placeholder user for orphans

### Migration is Slow

**Cause:** Large dataset (1000+ documents).

**Solution:**
- Migration runs in batches (progress logged every 10 users, 100 facts)
- Typical performance: ~50-100 documents/second
- For 10,000+ documents, consider running on Cloud Run or GCE

---

## Post-Migration Checklist

- [ ] Verify all users migrated (compare counts)
- [ ] Verify all facts migrated (compare counts)
- [ ] Verify accounts created (one per user)
- [ ] Spot-check 5-10 migrated documents (OAuth fields present)
- [ ] Test OAuth login flow with migrated users
- [ ] Test IAM permissions with migrated accounts
- [ ] Test fact visibility with migrated facts
- [ ] Update application to use `_oauth` collections
- [ ] Monitor for errors in production
- [ ] After 7 days stable: Archive old collections

---

## Collection Switching

After successful migration, update application to use OAuth collections:

```python
# src/config/environment.py or similar

# Before (old collections)
FIRESTORE_COLLECTION_PREFIX = "dev_"

# After (OAuth collections)
FIRESTORE_COLLECTION_PREFIX = "dev_"  # Still dev_, but use _oauth suffix in adapters

# Or update collection names directly
USER_COLLECTION = "dev_users_oauth"
ACCOUNT_COLLECTION = "dev_accounts_oauth"
FACT_COLLECTION = "dev_facts_oauth"
```

---

## FAQ

**Q: Can I run migration multiple times?**
A: Yes, but only if target collections are empty. Migration is not idempotent. Always run dry-run first.

**Q: What happens to old collections?**
A: Old collections remain untouched. After 7 days stable, you can archive or delete them.

**Q: Can I migrate incrementally?**
A: No, migration is all-or-nothing. OAuth architecture requires all entities migrated together.

**Q: What if migration fails mid-way?**
A: Delete partial data from `_oauth` collections (see Rollback Procedure) and re-run.

**Q: Can I rollback after going live?**
A: Yes, if migration just completed. Restore from backup or delete `_oauth` collections. If application has been running for days, rollback is complex (data divergence).

---

**Last Updated:** 2026-01-31 (Session 8)
**Status:** ✅ Migration Script Complete
