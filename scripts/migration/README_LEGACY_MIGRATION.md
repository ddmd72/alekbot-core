# Legacy Facts Migration to v3 Taxonomy

## Purpose

Migrate ~440 legacy facts (missing `domain` taxonomy) through **ConsolidationAgent v3** for proper 4D classification (Domain × Temporal × State × Priority).

## Strategy

1. **Fetch** oldest N legacy facts (`domain IS NULL`)
2. **Synthesize** user message: "На тобі факти про мене. Вони були класифіковані неефективно..."
3. **Process** через ConsolidationAgent v3 → 8-step deliberate process
4. **Mark** old facts as `SUPERSEDED`
5. **Repeat** until no legacy facts remain

## Prerequisites

```bash
# 1. Ensure ConsolidationAgent v3 is deployed and tested
# 2. Have account_id and user_id ready
# 3. Backup Firestore (safety first!)
```

## Usage

### Dry Run (Recommended First)

```bash
# Test on 20 facts without modifying database
python scripts/migration/reprocess_legacy_facts.py \
    --account-id YOUR_ACCOUNT_ID \
    --user-id YOUR_USER_ID \
    --dry-run \
    --batch-size 20
```

**Expected output:**

```
🚀 LEGACY FACTS MIGRATION
======================================================================
Account: YOUR_ACCOUNT...
User: YOUR_USER...
Batch size: 20
Mode: DRY RUN (no writes)
======================================================================

📊 Found 440 legacy facts to migrate

🔍 DRY RUN MODE - Facts will NOT be marked as SUPERSEDED

======================================================================
📦 BATCH 1 (20 facts)
======================================================================
   [1] User weighs 81kg...
   [2] User owns 2005 Mitsubishi Colt...
   [3] User is integrating Gemini API...
   ... and 17 more

🧠 Calling ConsolidationAgent v3...
✅ ConsolidationAgent completed: 15 operations
   - CREATE: New fact with domain=HEALTH, temporal=DYNAMIC, priority=...
   - UPDATE: Enriched existing car fact with new details...
   - MERGE: Consolidated 3 car facts into comprehensive fact...
   - DISCARD: Too vague - no actionable detail...

[DRY RUN] Would mark 20 facts as SUPERSEDED

📊 PROGRESS: 20/440 (4.5%)
⏱️  Elapsed: 2.1min | ETA: 44.3min
❌ Failed: 0
```

### Live Run (After Dry Run Validation)

```bash
# Process all facts and mark old as SUPERSEDED
python scripts/migration/reprocess_legacy_facts.py \
    --account-id YOUR_ACCOUNT_ID \
    --user-id YOUR_USER_ID \
    --live \
    --batch-size 20
```

**⚠️ WARNING:** Live mode will mark facts as `SUPERSEDED`. Ensure dry-run results look correct first.

## Parameters

| Parameter      | Required | Default | Description                          |
| -------------- | -------- | ------- | ------------------------------------ |
| `--account-id` | ✅       | -       | Account ID to migrate                |
| `--user-id`    | ✅       | -       | User ID (owner of facts)             |
| `--batch-size` | ❌       | 20      | Facts per batch (10-20 recommended)  |
| `--dry-run`    | One of   | -       | Preview mode (no writes)             |
| `--live`       | these    | -       | Execution mode (marks as SUPERSEDED) |

## What Happens During Migration?

### 1. Fact Fetching

```sql
SELECT * FROM domain_facts_v2
WHERE account_id = 'ACCOUNT_ID'
  AND domain IS NULL
  AND state != 'superseded'
ORDER BY created_at ASC
LIMIT 20
```

### 2. Synthetic Message

```
User: "На тобі факти про мене. Вони були класифіковані неефективно.
Хочу, щоб ти їх обробив ще раз та правильно класифікував за 4D таксономією.

Факти для рекласифікації:
1. User weighs 81kg
2. User owns 2005 Mitsubishi Colt
...
20. User is integrating Gemini API"
```

### 3. ConsolidationAgent v3 Processing

**8-Step Deliberate Process:**

1. **EXTRACT** → Parse facts from message
2. **CLASSIFY** → Assign 4D taxonomy (Domain, Temporal, State, Priority)
3. **SEARCH** → Query existing facts via `search_existing_facts` tool
4. **ANALYZE** → Compare candidate vs existing
5. **DECIDE** → UPDATE / CREATE / MERGE / DISCARD
6. **EXECUTE** → Call fact management tools
7. **VERIFY** → Check tool results
8. **REPORT** → Return operations JSON

**Example operations:**

```json
{
  "operations": [
    {
      "action": "CREATE",
      "fact_id": "new-uuid-123",
      "reason": "New fact with proper taxonomy (domain=HEALTH, temporal=DYNAMIC)"
    },
    {
      "action": "MERGE",
      "new_fact_id": "merged-uuid-456",
      "old_fact_ids": ["old-1", "old-2", "old-3"],
      "reason": "Consolidated 3 car facts into comprehensive fact"
    },
    {
      "action": "DISCARD",
      "reason": "Too vague - no actionable detail"
    }
  ]
}
```

### 4. Marking Old Facts

If **NOT** dry-run:

```python
for old_fact in batch:
    old_fact.state = FactState.SUPERSEDED
    old_fact.is_current = False
    old_fact.valid_to = datetime.now()
    await repo.update_fact(old_fact)
```

## Performance Estimates

**With batch_size=20:**

- 440 facts / 20 = **22 batches**
- ~120s per batch (ConsolidationAgent v3 deliberate process)
- **22 × 120s = 2640s ≈ 44 minutes**

**With batch_size=10:**

- 440 facts / 10 = **44 batches**
- ~90s per batch
- **44 × 90s = 3960s ≈ 66 minutes**

**Recommendation:** Start with `--batch-size 10` for dry-run, increase to `20` for live run if confident.

## Error Handling

**Critical errors → STOP migration:**

- ConsolidationAgent returns `success=False`
- Python exception in tool execution
- Firestore connection errors

**Progress saved:** Each batch is atomic. If migration stops, re-run with same parameters (script auto-resumes from first unprocessed fact).

## Rollback Plan

If migration produces bad results:

1. **Query superseded facts:**

```python
old_facts = await repo.get_facts_where(
    account_id="ACCOUNT_ID",
    state="superseded",
    valid_to__gte="2026-02-17"  # Today
)
```

2. **Restore state:**

```python
for fact in old_facts:
    fact.state = FactState.CURRENT
    fact.is_current = True
    fact.valid_to = None
    await repo.update_fact(fact)
```

3. **Delete new facts:**

```python
new_facts = await repo.get_facts_where(
    account_id="ACCOUNT_ID",
    created_at__gte="2026-02-17T00:00:00"
)

for fact in new_facts:
    await repo.delete_fact(fact.id)
```

## Monitoring

**Watch logs for:**

- ✅ Success: `"✅ ConsolidationAgent completed: N operations"`
- ❌ Failures: `"❌ ConsolidationAgent failed: error message"`
- 📊 Progress: `"📊 PROGRESS: X/440 (Y%)"`

**Check ConsolidationAgent decisions:**

- CREATE vs UPDATE vs MERGE ratio
- DISCARD reasons (should be valid)
- Tool call patterns (search → analyze → decide)

## FAQ

### Q: Can I run migration in parallel?

**A:** No. Script uses sequential batch processing. Running multiple instances will cause race conditions (same facts processed twice).

### Q: What if migration fails mid-way?

**A:** Re-run with same parameters. Script will resume from first unprocessed fact (query excludes already-processed facts).

### Q: How to verify results?

**A:** After dry-run, check operations log. After live-run, inspect random sample of new facts:

```python
new_facts = await repo.get_facts_where(
    account_id="ACCOUNT_ID",
    created_at__gte="2026-02-17T00:00:00",
    limit=10
)

for fact in new_facts:
    print(f"Domain: {fact.domain}, Temporal: {fact.temporal_class}, Priority: {fact.context_priority}")
    print(f"Text: {fact.text}\n")
```

### Q: Can I customize synthetic message?

**A:** Yes. Edit `_build_reclassification_message()` method in `reprocess_legacy_facts.py`.

### Q: What if ConsolidationAgent creates wrong taxonomy?

**A:** This is why dry-run exists. Review first batch carefully. If classifications are wrong, adjust prompt or provide examples in ConsolidationAgent v3 prompt.

## Success Criteria

**Dry-run validation:**

- ✅ 0 errors
- ✅ Operations make sense (CREATE/UPDATE/MERGE/DISCARD ratios reasonable)
- ✅ Classifications look correct (domain, temporal, priority)
- ✅ No duplicate facts created

**Live-run validation:**

- ✅ All 440 facts processed
- ✅ Old facts marked as SUPERSEDED
- ✅ New facts have proper taxonomy
- ✅ Biographical cache refreshed successfully

## Next Steps

After successful migration:

1. **Verify biographical cache:**

```python
cache = await repo.get_biographical_context_cached(account_id="ACCOUNT_ID")
print(f"Cached facts: {len(cache)}")
# Should include CRITICAL + HIGH priority facts
```

2. **Test search quality:**

```python
results = await repo.search_facts(
    query_vector=embedding,
    account_id="ACCOUNT_ID",
    limit=10
)
# Should return more relevant facts with proper taxonomy
```

3. **Monitor production:**

- Watch consolidation logs for next 1-2 days
- Ensure no regression in fact quality
- Verify cache refresh works correctly

---

**Last Updated:** 2026-02-17  
**Status:** ✅ Ready for Dry-Run Testing
