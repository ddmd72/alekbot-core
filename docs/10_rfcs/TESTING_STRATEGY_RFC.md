# RFC: Testing Strategy & Framework

**Status:** Active
**Author:** Alek-Core AI
**Date:** 19.01.2026

**Current Implementation:** Partial (pytest structure + RTM markers in tests)
**Gap:** Test coverage completeness unknown (needs audit)

## 1. Objective
Establish a robust, KISS-compliant testing framework that ensures high code quality, architectural integrity (Hexagonal), and business requirement traceability (RTM). The primary goal is to enable rapid and reliable sprint validation.

## 2. Testing Levels

### 2.1. Unit Tests (L1)
- **Scope:** Individual functions, domain entities, and isolated logic.
- **Mocking:** 100% isolation. No external dependencies.
- **Goal:** Verify atomic logic and edge cases.
- **Traceability:** Linked to REQ-IDs where applicable.

### 2.2. Integration Tests (L2)
- **Scope:** Interaction between components (e.g., Service -> Port -> Adapter).
- **Mocking:** External services (Slack API, Gemini API) are mocked. Infrastructure (Firestore Emulator) is used where possible.
- **Goal:** Verify that components work together correctly.
- **Traceability:** Linked to REQ-IDs.

### 2.3. System Tests (L3)
- **Scope:** End-to-end flows within the application (e.g., Message -> Brain -> Tool -> Response).
- **Mocking:** Minimal. Uses Firestore Emulator and Mocked Slack/Gemini clients.
- **Goal:** Verify complete business features.
- **Traceability:** Mandatory link to REQ-IDs.

### 2.4. Manual/Automated E2E (L4)
- **Scope:** Real interaction with Slack in the `development` environment.
- **Goal:** Final UAT before production.

## 3. Tooling & Standards
- **Framework:** `pytest` + `pytest-asyncio`.
- **Mocking:** `unittest.mock` or `pytest-mock`.
- **Reporting:** 
  - `junitxml` for machine-readable results.
  - `report.md` (custom generated) for human-readable RTM status.
- **Naming:** `test_<requirement_id>_<description>.py`.

## 4. Mocking Strategy (Hexagonal)
- **Ports are the boundaries.** We mock the implementation of Ports (`LLMService`, `FactRepository`) when testing the Application Layer (`BrainService`, `ConversationHandler`).
- **Adapters are tested against Emulators** (Firestore) or specific Mocks (Slack WebClient).

## 5. Traceability (RTM)
Every test file must contain a docstring or a marker indicating which Requirement ID it covers.
Example:
```python
@pytest.mark.requirement("REQ-MEM-01")
async def test_scd_type_2_versioning():
    ...
```

## 6. Execution Plan
1. **Phase 1:** Infrastructure setup (Pytest config, base mocks).
2. **Phase 2:** Unit tests for Domain & Utils.
3. **Phase 3:** Integration tests for Repositories (Firestore Emulator).
4. **Phase 4:** System tests for BrainService (Mocked LLM).
5. **Phase 5:** RTM Reporting automation.

## 7. Success Criteria
- [ ] `make test` executes all levels.
- [ ] Test report generated in `tests/reports/`.
- [ ] 100% coverage of CORE and MEM requirements.

## 8. Test Failure Investigation Protocol

### 8.1. Golden Rule
**Tests are the specification of system behavior.**
Changing a test WITHOUT a full investigation is masking a bug.

### 8.2. Investigation Workflow (mandatory)

#### Step 1: Stop & Analyze
- [ ] Read the error message IN FULL
- [ ] Understand **what was expected** vs **what was received**
- [ ] Do NOT change the test or the code until the root cause is found

#### Step 2: Investigate Context
- [ ] Read the code under test (implementation)
- [ ] Read the test code (expectations)
- [ ] Check `CHANGELOG.md` — was the behavior changed intentionally?
- [ ] Check git history (`git log --oneline <file>`)
- [ ] Check `REQUIREMENTS.md` — what does the business logic say?

#### Step 3: Root Cause Classification

**A. Test is outdated (code is correct, test is stale)**
- ✅ There is an entry in `CHANGELOG.md` about the behavior change
- ✅ The code behavior matches the current requirements
- ✅ The test expects the old logic
→ **Action:** Update the test + add a comment with a reference to the CHANGELOG

**B. Code is broken (test is correct, code has a bug)**
- ❌ The test expects behavior from `REQUIREMENTS.md`
- ❌ The code does NOT satisfy the requirement
- ❌ No justification in `CHANGELOG.md`
→ **Action:** Fix the CODE, NOT the test

**C. Requirements changed (both test and code are stale)**
- 🔄 The business requirement has changed
- 🔄 A new requirement ID is needed
→ **Action:** Update `REQUIREMENTS.md` → update the test → update the code

**D. Requirement missing (logic without documentation)**
- 📋 The code implements behavior
- 📋 No REQ-ID in `REQUIREMENTS.md`
→ **Action:** Add the requirement → create/update the test

#### Step 4: Document Decision
Every test change must include:
- A reference to the CHANGELOG entry
- A reference to the REQ-ID
- A comment in the test: `# Updated 2026-01-XX: <reason> (see CHANGELOG)`

### 8.3. Anti-Patterns (forbidden)
- ❌ Changing a test "to make it pass" without understanding why
- ❌ Changing a test without checking `CHANGELOG.md`
- ❌ Changing a test without checking `REQUIREMENTS.md`
- ❌ Rushing — it is better to spend time investigating than to mask a bug

### 8.4. Encouraged Practices
- ✅ Read documentation before changing a test
- ✅ Read the codebase to understand the context
- ✅ Consult `CHANGELOG.md`
- ✅ Consult git history
- ✅ Deep investigation is worth more than speed

## 9. Requirements-Driven Development Process

### 9.1. Workflow (strictly in this order)

#### Phase 1: Requirements
1. A new feature starts with `REQUIREMENTS.md`
2. Add a REQ-ID with business value (The "Why")
3. Describe the logic mapping
4. Get confirmation from the owner

#### Phase 2: Tests
1. Create a failing test (Red)
2. The test must verify the REQ-ID
3. The test must have `@pytest.mark.requirement("REQ-XXX")`
4. Define the level (Unit/Integration/System)

#### Phase 3: Code
1. Implement the minimum code to make the test pass (Green)
2. Follow Hexagonal Architecture

#### Phase 4: Refactor
1. Improve the code without changing behavior
2. Tests must remain GREEN

#### Phase 5: Validation
1. Run `make test`
2. Run UAT in dev
3. Update CHANGELOG
4. Update docs

### 9.2. Gap Detection
If code is found without requirements:
1. Stop
2. Add a requirement to `REQUIREMENTS.md`
3. Create a test
4. Verify that the code satisfies the requirement
5. Document it

## 10. Next Test Session Instruction (Must be Unambiguous)

**When a new test session starts, immediately do the following:**
1. **Read** `docs/management/REQUIREMENTS.md` and `tests/reports/coverage_gap_report.md`.
2. **Review** `docs/management/TEST_COVERAGE_PLAN.md` for the current workflow.
3. **Run** tests in this order: `pytest tests/unit`, `pytest tests/integration`, `pytest tests/system`, then `make test`.
4. **If any test fails**, follow Section 8 (Test Failure Investigation Protocol) step‑by‑step.
5. **If gaps remain**, continue Phase 2–4 of the Test Coverage Plan (gap analysis → requirements → tests).
6. **Document** outcomes in `tests/reports/coverage_gap_report.md`.

This instruction is mandatory for any new test-focused session to ensure immediate, consistent execution.
