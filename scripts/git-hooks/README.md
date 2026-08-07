# Git Security Hooks

Automated checks to prevent commits/pushes containing sensitive data (PII, real bank/company names, keywords revealing security incidents).

## Installation

```bash
./scripts/install-git-hooks.sh
```

This copies hooks to `.git/hooks/` and makes them executable. Should be run once after cloning or whenever hooks are updated.

## Pre-Commit Hook

**Location:** `scripts/git-hooks/pre-commit`

Runs before `git commit`. Blocks commits if:

### 1. Commit Message Keywords
Detects keywords that reveal a data leak/privacy incident:
- `privacy scrub`, `privacy cleanup`
- `real data`, `real bank`, `real company`, `real name`, `real address`
- `breach`, `data leak`, `PII`
- `remove owner personal`, `replace owner`, `sanitize`

**Good commit messages:**
```
✓ fix: standardize test fixture formats
✓ refactor: update sample data
✓ chore: consolidate data values
```

**Bad commit messages:**
```
✗ fix(privacy): replace real bank account data
✗ chore: remove owner's personal information
✗ merge: personal-data privacy scrub
```

### 2. Real Bank Names
Blocks 40+ real bank names (across Europe, USA, fintech):
- Spain: Santander, BBVA, CaixaBank, Sabadell, etc.
- UK: HSBC, Barclays, Lloyds, etc.
- USA: JPMorgan Chase, Bank of America, Wells Fargo, etc.
- Fintech: Revolut, Wise, N26, etc.

**Exception:** Bank names are allowed in:
- Documentation files (`docs/`)
- Test files (`tests/`)
- Generic example phrases: `"User works at Google"`, `"Example Bank"`

See `lists/allowed-phrases.txt` and `lists/allowed-files.txt`.

### 3. Real Company Names
Blocks 100+ real company names:
- Tech: Google, Microsoft, Apple, Amazon, Meta, Netflix, etc.
- Finance: PayPal, Stripe, Wise, Revolut, etc.
- Telecom: Vodafone, Orange, Deutsche Telekom, etc.
- Airlines: Lufthansa, Ryanair, KLM, etc.

**Exception:** Same as banks — allowed in docs/tests or generic examples.

### 4. PII Patterns
Detects real-world identifiers:

| Pattern | Example | Blocks |
|---------|---------|--------|
| IBAN | `ES91 21** **** **** **** ****` | International bank account numbers |
| Credit card | `4532 1111 **** ****` | 16-digit card patterns |
| VIN | `17-character vehicle ID` | Vehicle ID numbers |
| Tracking number | `UPU S10 format (LT + 9 digits + country)` | Postal/customs tracking |
| Phone number | `+XX XXX XXX XXXX` | `+` followed by 7-15 digits |

## Pre-Push Hook

**Location:** `scripts/git-hooks/pre-push`

Runs before `git push`. For pushes to `main`:

1. Shows commits being pushed (`git log`)
2. Shows changed files (`git diff --stat`)
3. Requires explicit confirmation: type `yes` to proceed
4. Blocks push if anything else is typed or Ctrl-C is pressed

For other branches, push proceeds without confirmation.

**Example:**
```
═══════════════════════════════════════════════════════════
PUSHING TO MAIN BRANCH
═══════════════════════════════════════════════════════════

Commits being pushed:
  d72417f feat: add git security hooks
  e93e905 fix: standardize test fixture formats

Files changed:
  scripts/git-hooks/pre-commit            | 234 ++
  scripts/git-hooks/pre-push              | 108 ++
  ...

Are you sure you want to push to main?
Type 'yes' to confirm or press Ctrl-C to cancel: 
```

## Override / Bypass

**Not recommended.** Only for CI or special cases.

### Skip pre-commit
```bash
git commit --no-verify
```

### Skip pre-push
```bash
git push --no-verify
```

Or:
```bash
SKIP_PUSH_CHECK=1 git push origin main
```

## Configuration Files

### `lists/real-banks.txt`
List of 40+ bank names (one per line). Add new banks here if needed.

### `lists/real-companies.txt`
List of 100+ company names (one per line). Add new companies here if needed.

### `lists/allowed-phrases.txt`
Regex patterns where company/bank names are allowed (e.g., `"User works at Google"`).
Used to whitelist generic examples in documentation.

### `lists/allowed-files.txt`
File path patterns where real names are allowed (e.g., `docs/`, `tests/`, `CLAUDE.md`).
Avoids false positives in examples and test data.

## How It Works

### Pre-Commit Flow

```
git commit
  ↓
.git/hooks/pre-commit (runs)
  ├─ Parse commit message
  ├─ Check for sensitive keywords
  │   └─ If found → BLOCK
  ├─ Get staged file diff (git diff-index --cached)
  ├─ For each file:
  │   ├─ Check for real bank names
  │   │   └─ If in allowed file/phrase → SKIP
  │   │   └─ Else if found → BLOCK
  │   ├─ Check for real company names (same logic)
  │   ├─ Check for IBAN/VIN/credit card patterns
  │   │   └─ If found → BLOCK
  │   └─ Check for phone numbers
  │       └─ If found → BLOCK
  └─ If all OK → allow commit
     If issues found → BLOCK, show errors
```

### Pre-Push Flow

```
git push origin main
  ↓
.git/hooks/pre-push (runs)
  ├─ Check if pushing to main
  ├─ If yes:
  │   ├─ Show commits (git log)
  │   ├─ Show files (git diff --stat)
  │   ├─ Ask for confirmation
  │   └─ If 'yes' typed → allow push
  │   └─ Else → BLOCK
  └─ If other branch → allow push
```

## Testing

Test that hooks work:

```bash
# This should fail (keyword in message)
git commit -m "fix(privacy): remove owner real data" --allow-empty

# This should fail (real company name)
echo "Google is hiring" >> README.md
git add README.md
git commit -m "update readme"

# This should pass (generic message, generic file)
echo "This is a test" >> test_example.py
git add test_example.py
git commit -m "add example test"
```

## Notes

- Hooks run locally; they are not enforced on the server
- Patterns are designed to catch real leaks, not all possible issues
- False positives can occur; adjust `allowed-*` lists if needed
- Hooks run on every commit/push; overhead is minimal (<100ms)

## References

- IBAN format: https://en.wikipedia.org/wiki/International_Bank_Account_Number
- VIN format: https://en.wikipedia.org/wiki/Vehicle_identification_number
- UPU tracking: https://en.wikipedia.org/wiki/Universal_Postal_Union
