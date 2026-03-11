# Session Protocol: OAuth Multi-Tenant Session 8 Debugging

**Date:** 2026-01-31
**Session ID:** Session 8 Debugging
**Context:** Debugging Slack bot after OAuth multi-tenant refactoring
**Branch:** develop
**Status:** ✅ Complete

---

## Session Overview

Debugging session after OAuth Multi-Tenant implementation (Sessions 1-10). Bot stopped responding after refactoring due to Pydantic validation errors and missing Firestore indexes.

---

## Problems Identified & Fixed

### Problem 1: Pydantic Validation Errors

**Root Cause:**
- System facts in Firestore have old schema with `owner_id`
- New code expects OAuth fields: `account_id`, `created_by_user_id`, `visibility`
- FactEntity Pydantic validation fails when loading old data

**Symptoms:**
```python
3 validation errors for FactEntity
account_id - Field required
created_by_user_id - Field required
visibility - Input should be 'account_shared' or 'user_private' [got 'private']
```

**Solution:**
- Added `_migrate_ownership_fields()` method in [firestore_repo.py](../../src/adapters/firestore_repo.py)
- Automatic migration on data load:
  - `owner_id` → `account_id` + `created_by_user_id`
  - `visibility='private'` → `visibility='user_private'`
- Updated all query methods with backward compatibility:
  - Try `account_id` first
  - Fallback to `owner_id` for legacy data

**Files Modified:**
- `src/adapters/firestore_repo.py` - Added migration logic to 12 methods

---

### Problem 2: Missing Vector Indexes

**Root Cause:**
- Vector indexes only existed for `owner_id` field
- New queries use `account_id` field
- Biographical context queries failing with "Missing vector index configuration"

**Solution:**
- Added 2 new vector index definitions to `config/firestore.indexes.json`:
  - `development_facts`: `account_id + is_current + vector`
  - `facts`: `account_id + is_current + vector`
- Deployed indexes via gcloud CLI
- Indexes built successfully (READY status)

**Files Modified:**
- `config/firestore.indexes.json` - Added 2 new index definitions

---

### Problem 3: UserProfile.usage Attribute Error

**Root Cause:**
- `firestore_user_repo.py:158` trying to access `user.usage`
- After OAuth refactor, usage tracking moved from user to account level
- UserProfile no longer has `usage` field

**Symptoms:**
```python
'UserProfile' object has no attribute 'usage'
```

**Solution:**
- Simplified `increment_usage()` method in `firestore_user_repo.py`
- Removed all user-level usage tracking
- Delegated to `account_repo.increment_account_usage()`

**Files Modified:**
- `src/adapters/firestore_user_repo.py` - Removed user.usage access

---

### Problem 4: Biographical Context Cache Regeneration Loop

**Root Cause:**
- Empty cache `[]` treated as invalid due to `if facts:` check in `firestore_repo.py:498`
- Cache validation logic fell through to regeneration when cache was empty
- Empty cache is valid (means user has no biographical facts)

**Symptoms:**
```
⚠️ [Cache] Missing or invalid for {owner_id}..., generating automatically...
```
Appearing on every message, causing performance degradation.

**Solution:**
- Removed `if facts:` condition in `get_biographical_context_cached()` method
- Empty cache now properly treated as valid state
- No more unnecessary cache regeneration

**Files Modified:**
- `src/adapters/firestore_repo.py:498` - Removed empty cache invalidation logic

---

### Problem 5: Test Session Cleanup Not Working

**Root Cause:**
- Integration tests cleanup searched for `user_id` field in sessions
- Sessions don't have `user_id` field (only `owner_id`, and old sessions have neither)
- Document structure: `created_at`, `expires_at`, `history`, `last_activity`, `message_count`, `updated_at`
- Three types of test sessions identified by document ID pattern:
  1. Timestamps: `1769025250.341749`
  2. Test E2E: `session_20260125_024105`
  3. Validation: `session_validation_test_user_{hash}`
- Real user sessions use UUID format and should be preserved

**Solution:**
- Changed cleanup field from `user_id` to `owner_id` in test_sliding_window_e2e.py
- Added session cleanup to test_consolidation_e2e.py (was missing)
- Created standalone cleanup script using document ID pattern matching
- Updated Makefile to call standalone script (fixed inline Python syntax error)

**Files Modified:**
- `tests/integration/test_sliding_window_e2e.py:406` - Changed `user_id` to `owner_id`
- `tests/legacy/test_consolidation_e2e.py:204-208` - Added session cleanup
- `scripts/cleanup_test_sessions.py` - NEW standalone cleanup script
- `Makefile:220-225` - Updated clean-test-sessions to use standalone script

---

### Problem 6: Duplicate Bot Processes

**Root Cause:**
- Multiple bot processes running simultaneously (12 instances)
- Slack rejecting connections with "too_many_websockets" error

**Solution:**
- Killed all processes: `pkill -9 -f "main.py"`
- Added `make kill-local` command to Makefile for quick cleanup

**Files Modified:**
- `Makefile:214-218` - Added `kill-local` target

---

## Technical Decisions

### Decision 1: Runtime Migration vs Data Migration

**Choice:** Runtime migration (automatic on data load)

**Rationale:**
- Simpler deployment (no migration script execution)
- Gradual migration as data is accessed
- Zero downtime
- Old data remains untouched in Firestore
- Hexagonal Architecture compliance (migration in Adapter layer)

**Trade-off:**
- Small performance overhead on first load
- Both schemas coexist temporarily

---

### Decision 2: Backward Compatibility Strategy

**Choice:** Try new field first, fallback to old field

**Implementation:**
```python
# Try account_id
query = self.facts_col.where(filter=FieldFilter("account_id", "==", owner_id))
docs = await query.get()

# Fallback to owner_id
if not docs:
    query = self.facts_col.where(filter=FieldFilter("owner_id", "==", owner_id))
    docs = await query.get()
```

**Rationale:**
- Supports both old and new data
- Performance: new data found on first try
- Legacy data still works without migration
- Safe rollback path

---

## Code Changes Summary

| File | Changes | Lines | Commit |
|------|---------|-------|--------|
| `src/adapters/firestore_repo.py` | Added `_migrate_ownership_fields()`, updated 12 query methods, fixed cache loop | ~150 | TBD |
| `src/adapters/firestore_user_repo.py` | Removed `user.usage` access, delegated to account repo | ~15 | TBD |
| `config/firestore.indexes.json` | Added 2 vector index definitions for `account_id` | +18 | TBD |
| `Makefile` | Added `kill-local` target, updated `clean-test-sessions` to use standalone script | +6 | TBD |
| `src/utils/logger.py` | Added file handler for debug logging | ~20 | TBD |
| `tests/integration/test_sliding_window_e2e.py` | Fixed cleanup field from `user_id` to `owner_id` | 1 | TBD |
| `tests/legacy/test_consolidation_e2e.py` | Added session cleanup | +5 | TBD |
| `scripts/cleanup_test_sessions.py` | NEW: Standalone cleanup script for test sessions | +38 | TBD |

---

## Testing Results

### Manual Testing
- ✅ Bot connects to Slack Socket Mode successfully
- ✅ Message events reach handler and are processed
- ✅ Pydantic validation works with automatic migration
- ✅ Backward compatibility for `owner_id` → `account_id` queries
- ✅ Visibility field migration `'private'` → `'user_private'`
- ✅ Usage tracking at account level only
- ✅ Vector indexes READY status
- ✅ Biographical context loads without errors
- ✅ Biographical context cache no longer regenerates on every message
- ✅ Test session cleanup script works with document ID pattern matching
- ✅ Makefile `clean-test-sessions` command executes successfully

### Log Evidence
```
✅ Socket Mode connected (session: s_285634150)
✅ Message processed and response sent
✅ No validation errors
✅ Migrated legacy owner_id to account_id/created_by_user_id
✅ Biographical context cache not triggering regeneration warnings
```

---

## Lessons Learned

### Architecture
1. **Hexagonal Architecture saved us:**
   - Migration logic in Adapter layer (not Domain)
   - Domain entities unchanged
   - Backward compatibility without Domain pollution

2. **Runtime migration is powerful:**
   - No complex deployment procedures
   - Gradual migration as data is accessed
   - Zero downtime

### Operations
1. **Debug logging is essential:**
   - Added comprehensive file logging (`alek_debug.log`)
   - Saved hours of debugging time

2. **Process management matters:**
   - Need quick way to kill duplicate processes
   - Added `make kill-local` for convenience

3. **Vector indexes are critical:**
   - Must update indexes when query fields change
   - Index creation takes 5-15 minutes
   - Plan ahead for zero-downtime deployments

4. **Inspect data before fixing:**
   - Always check actual Firestore documents first
   - Don't assume field names or structure
   - Screenshot evidence prevents wrong assumptions
   - User feedback: "Tell me about the problem first, then fix it"

5. **Document ID patterns for cleanup:**
   - More reliable than field-based queries
   - Works with old and new schema
   - Preserves real user data (UUID format)
   - Standalone scripts better than inline Make commands

6. **Cache validation must handle empty state:**
   - Empty cache can be valid state
   - Don't conflate "empty" with "invalid"
   - Prevents infinite regeneration loops

---

## Follow-Up Actions

### Immediate (Done)
- ✅ Fixed Pydantic validation errors
- ✅ Deployed vector indexes
- ✅ Fixed usage tracking
- ✅ Added process cleanup command
- ✅ Fixed biographical context cache regeneration loop
- ✅ Created standalone test session cleanup script
- ✅ Updated Makefile to use standalone cleanup script
- ✅ Fixed test cleanup in integration tests

### Short-Term (TODO)
- [ ] Run cleanup script on actual development_sessions collection
- [ ] Update documentation:
  - Building block docs affected by changes
  - Add migration strategy to OAuth guides
- [ ] Add Session Context to IMPLEMENTATION_ROADMAP.md
- [ ] Commit all changes with proper commit message

### Long-Term (TODO)
- [ ] Monitor biographical context performance with new indexes
- [ ] Consider data migration script for production deployment
- [ ] Add automated tests for backward compatibility
- [ ] Document runtime migration pattern for future refactorings
- [ ] Add automated test session cleanup in CI/CD pipeline

---

## Key Metrics

| Metric | Value |
|--------|-------|
| Problems Fixed | 6 |
| Files Modified | 8 |
| Methods Updated | 12 |
| Indexes Added | 2 |
| Scripts Created | 1 |
| Debugging Time | ~4 hours |
| Bot Downtime | 0 (local dev only) |

---

**Last Updated:** 2026-01-31
**Status:** ✅ Complete - Bot fully operational with OAuth schema
