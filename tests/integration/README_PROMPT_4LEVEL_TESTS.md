# Prompt 4-Level Integration Tests

SESSION_26: End-to-end tests for USER > ACCOUNT > AGENT > SYSTEM priority resolution.

## Files

- **test_prompt_4level_e2e.py** - Real integration test with Firestore
- **ai_templates/components/account/test_master_account/** - Test account components
- **ai_templates/components/user/test_dev_user/** - Test user components

## Prerequisites

1. **Test Environment Setup:**
   ```bash
   export APP_ENV=test
   export GOOGLE_CLOUD_PROJECT=your-test-project
   ```

2. **Upload Test Components to Firestore:**
   ```bash
   # Upload account-level test component
   python scripts/prompt/sync_components.py --env test --level account --account-id test_master_account

   # Upload user-level test component
   python scripts/prompt/sync_components.py --env test --level user --user-id test_dev_user
   ```

## Running Tests

### Run all E2E tests:
```bash
pytest tests/integration/test_prompt_4level_e2e.py -v -s
```

### Run specific scenario:
```bash
# Scenario 1: SYSTEM + ACCOUNT → ACCOUNT wins
pytest tests/integration/test_prompt_4level_e2e.py::test_e2e_system_plus_account -v -s

# Scenario 2: SYSTEM + USER → USER wins
pytest tests/integration/test_prompt_4level_e2e.py::test_e2e_system_plus_user -v -s

# Scenario 3: ALL levels → USER wins
pytest tests/integration/test_prompt_4level_e2e.py::test_e2e_all_levels_user_wins -v -s
```

### Run with integration marker:
```bash
pytest -m integration tests/integration/test_prompt_4level_e2e.py -v
```

## Test Flow

```
┌─────────────────────────────────────────────────────────┐
│ 1. Upload Components to Firestore (test_* collections)  │
│    - SYSTEM: properties (default)                       │
│    - AGENT: properties (smart agent override)           │
│    - ACCOUNT: properties (master account)               │
│    - USER: properties (dev user)                        │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ 2. Call PromptBuilder.build_for_agent()                 │
│    - agent_type="smart"                                 │
│    - account_id="test_master_account"                   │
│    - user_id="test_dev_user"                            │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ 3. Resolution Chain (REAL Firestore queries)            │
│    USER > ACCOUNT > AGENT > SYSTEM                      │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ 4. Assembly (REAL GroovyPromptAssembler)                │
│    Groovy prompt with correct overrides                 │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ 5. Verification                                          │
│    Assert: USER content present                         │
│    Assert: ACCOUNT/AGENT/SYSTEM NOT present             │
└─────────────────────────────────────────────────────────┘
```

## Expected Results

### Scenario 1 (SYSTEM + ACCOUNT):
```groovy
properties {
    archetype: "ACCOUNT_MASTER_OVERRIDE"  // ✅ ACCOUNT wins
    humor_engine {
        preset: "family_friendly"
    }
}
```

### Scenario 2 (SYSTEM + USER):
```groovy
properties {
    archetype: "USER_DEV_OVERRIDE"  // ✅ USER wins
    humor_engine {
        preset: "ranevskaya"
    }
}
```

### Scenario 3 (ALL levels):
```groovy
properties {
    archetype: "USER_DEV_OVERRIDE"  // ✅ USER wins (highest priority)
    humor_engine {
        preset: "ranevskaya"
    }
}
```

## Cleanup

Test cleanup happens automatically via fixture teardown, but you can manually clean:

```bash
# Delete test components from Firestore
firebase firestore:delete test_prompt_components \
  --where 'owner_value==test_master_account' \
  --project your-test-project

firebase firestore:delete test_prompt_components \
  --where 'owner_value==test_dev_user' \
  --project your-test-project
```

## Notes

- **No mocks**: Uses real Firestore, real Repository, real Service, real Builder
- **Test isolation**: Each test uploads its own components
- **Auto cleanup**: Fixture removes test data after all tests complete
- **Backdoor NOT tested**: Scenario 4 (AGENT protection) intentionally skipped - known issue for RFC v3

## Troubleshooting

**Test skipped with "GOOGLE_CLOUD_PROJECT not set":**
- Set environment variable: `export GOOGLE_CLOUD_PROJECT=your-project-id`

**Components not found:**
- Run sync script first (see Prerequisites)
- Check Firestore console for test_prompt_components collection

**Wrong overrides in assembled prompt:**
- Check component `owner_value` matches test constants
- Verify `is_enabled=true` in Firestore
- Clear cache: restart test or set `cache_ttl=0`
