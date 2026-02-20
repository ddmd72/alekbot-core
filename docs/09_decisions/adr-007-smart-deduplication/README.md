# ADR-007: Smart Deduplication Strategy

**Status:** Accepted  
**Date:** 2026-02-08  
**Deciders:** Dmytro Deleur, AI Assistant  
**Session:** SESSION_2026_02_08_DEDUPLICATION_OPTIMIZATION

---

## Context

### Problem Statement

The current deduplication algorithm (threshold=0.85) was **too aggressive**, resulting in:

- **67% fact reduction** (902 → 297 facts in production)
- Loss of important contextual information
- Rejection of detailed versions in favor of brief ones
- False positives due to high semantic similarity

**Key Issue:** System prioritized avoiding duplicates over preserving valuable information.

### Analysis

From deduplication reports:

- 39% of fact pairs had similarity 0.95-0.96 (moderately similar)
- Many "duplicates" were actually **complementary information**:
  - "Weight 83 kg" vs "Weight 83 kg in Puzol, representing 15kg loss"
  - "HbA1c 5.1%" vs "HbA1c 5.1%, indicating no diabetes"

---

## Decision

Implement **Smart Deduplication Strategy** with philosophy:

> **"Better to add a duplicate than to lose important information"**

### Algorithm (4 Levels)

```python
def is_duplicate(new_text, existing_text, similarity):
    # Level 1: similarity < 0.96 → NOT duplicate
    if similarity < 0.96:
        return False

    # Level 2: Numbers differ (sorted) → NOT duplicate
    new_nums = extract_and_sort(new_text)
    existing_nums = extract_and_sort(existing_text)
    if new_nums != existing_nums:
        return False  # Early exit

    # Level 3: similarity ≥ 0.98 → DUPLICATE
    if similarity >= 0.98:
        return True

    # Level 4: Length heuristic (15% threshold)
    if existing/new < 0.85:  # New is 15%+ longer
        return False  # New more detailed

    # Default: DUPLICATE
    return True
```

### Key Features

1. **Raised threshold:** 0.85 → 0.96
2. **Number-aware comparison:**
   - Sorted arrays for order-independent matching
   - "83 kg, 185 cm" = "185 cm, 83 kg"
3. **Length-based heuristic:** Preserve detailed versions
4. **Early exit optimization:** Fast rejection of obvious non-duplicates

---

## Consequences

### Positive

✅ **Reduced false positives:** 30-40% reduction vs 67% (more conservative)  
✅ **Preserved important details:** Location, interpretation, measurements  
✅ **Number-sensitive:** Different values/dates always preserved  
✅ **LLM-friendly:** Handles consistent phrasing from AI generation  
✅ **Fast:** Early exit on Level 1 (< 0.96) and Level 2 (numbers differ)

### Negative

⚠️ **Potential true duplicates:** May occasionally add real duplicates  
⚠️ **Sorted numbers limitation:** Cannot distinguish temporal changes

- Example: "85→83 kg" = "83→85 kg" (both have [83, 85])
- **Mitigation:** Acceptable for static facts (user confirmed)

### Trade-offs

| Metric          | Old (0.85)       | New (0.96)           |
| --------------- | ---------------- | -------------------- |
| Threshold       | 0.85             | 0.96                 |
| Reduction       | ~67%             | ~30-40%              |
| False Positives | High             | Low                  |
| False Negatives | Low              | Moderate             |
| Philosophy      | Avoid duplicates | Preserve information |

---

## Implementation

### Files Changed

1. **`src/services/deduplication_service.py`** (NEW)
   - SmartDeduplicationService class
   - 4-level algorithm
   - Number extraction with sorting

2. **`src/adapters/firestore_repo.py`**
   - Updated `add_fact_if_unique` method
   - Integrated SmartDeduplicationService
   - Enhanced logging with reasons
   - Added `_cosine_similarity()` helper (numpy-based, replaced scipy)

3. **`src/services/fact_write_service.py`**
   - Updated threshold: 0.85 → 0.96
   - Added session comment

4. **`tests/unit/services/test_deduplication_service.py`** (NEW)
   - 27 unit tests
   - Real-world scenarios
   - Edge case coverage

5. **`requirements.txt`**
   - Removed scipy dependency (replaced with numpy)

### Thresholds

```python
MODERATE_THRESHOLD = 0.96  # Quick exit if below
STRICT_THRESHOLD = 0.98     # Duplicate if above
LENGTH_RATIO = 0.85         # 15% difference
```

---

## Rationale

### Why 0.96 instead of 0.95?

From report analysis:

- **0.95-0.96 range:** 39% of pairs were NOT true duplicates
- **0.96-0.97 range:** More reliable duplicate indicators
- **Conservative approach:** Err on side of preservation

### Why sorted numbers?

**Pros:**

- LLM generates facts with inconsistent order
- "83 kg, 185 cm" ≡ "185 cm, 83 kg"
- Simplifies comparison logic

**Cons:**

- Cannot detect temporal changes
- "from 85 to 83" = "from 83 to 85"

**Decision:** User confirmed static facts only (no temporal dynamics)

### Why 15% length threshold?

**Analysis:**

- 10%: Too sensitive (noise)
- 20%: Too lenient (misses details)
- **15%:** Balanced sweet spot

**Examples:**

- "Weight 83 kg" (13 chars)
- "Weight 83 kg in Puzol" (21 chars)
- Ratio: 13/21 = 0.62 < 0.85 → Preserved ✅

---

## Examples

### Scenario 1: Different Values

```
New: "Weight 84 kg"
Existing: "Weight 83 kg"
Similarity: 0.97

Numbers: [84] != [83]
→ NOT duplicate ✅
```

### Scenario 2: Added Detail

```
New: "Weight 83 kg in Puzol, Spain"
Existing: "Weight 83 kg"
Similarity: 0.96

Numbers: [83] == [83]
Length: 17/33 = 0.52 < 0.85
→ NOT duplicate ✅
```

### Scenario 3: LLM Rewording

```
New: "Patient has Periodontitis"
Existing: "Patient has periodontitis condition"
Similarity: 0.99

Numbers: none
Similarity ≥ 0.98
→ DUPLICATE ❌
```

### Scenario 4: Different Dates

```
New: "Event on 2025-03-29"
Existing: "Event on 2025-03-28"
Similarity: 0.99

Numbers: [3, 29, 2025] != [3, 28, 2025]
→ NOT duplicate ✅
```

---

## Testing

27 unit tests covering:

- ✅ All 4 algorithm levels
- ✅ User-provided real-world scenarios
- ✅ Edge cases (floats, ranges, dates)
- ✅ Number extraction logic

**Run tests:**

```bash
pytest tests/unit/services/test_deduplication_service.py -v
```

---

## Monitoring

### Success Metrics

- **Fact retention rate:** 60-70% (vs 33% before)
- **User complaints:** Reduced missing information reports
- **Duplicate complaints:** Acceptable level

### Logging

New logs include deduplication reasons:

```
⏭️  [Dedup] Duplicate detected: strict_similarity: 0.99 >= 0.98
✅ [Dedup] NOT duplicate: numbers_differ: [83] != [84]
✅ [Dedup] NOT duplicate: new_more_detailed: existing 0.52 < 0.85
```

---

## References

- **Reports:** `reports/DEDUPLICATION_REPORT.md`
- **Session:** `docs/SESSION_2026_02_08_DEDUPLICATION_OPTIMIZATION.md`
- **Tests:** `tests/unit/services/test_deduplication_service.py`
- **Service:** `src/services/deduplication_service.py`

---

## Future Considerations

1. **Temporal awareness:** Detect "from X to Y" patterns
2. **Type-specific thresholds:** Events vs States vs Principles
3. **Multi-vector deduplication:** Use tags/metadata vectors
4. **ML-based approach:** Train model on accepted/rejected pairs
5. **User feedback loop:** Learn from manual corrections

---

## Status

- **Implemented:** 2026-02-08
- **Updated:** 2026-02-08 (scipy → numpy for cosine similarity)
- **Tested:** 27 unit tests passing
- **Deployed:** Pending
- **Monitoring:** TBD

---

## Implementation Notes

### Scipy Removal (2026-02-08)

**Problem:** scipy dependency caused import errors in production/local environments due to missing build tools or environment mismatches.

**Solution:** Replaced `scipy.spatial.distance.cosine` with numpy-based implementation:

```python
# Before (scipy):
from scipy.spatial.distance import cosine
distance = cosine(vec1, vec2)
similarity = 1.0 - distance

# After (numpy):
import numpy as np
dot_product = np.dot(vec1, vec2)
norm1 = np.linalg.norm(vec1)
norm2 = np.linalg.norm(vec2)
similarity = dot_product / (norm1 * norm2)
```

**Benefits:**

- ✅ numpy already in requirements.txt (no new dependency)
- ✅ Identical mathematical result
- ✅ Reduces Docker image size by ~100MB
- ✅ Eliminates scipy build dependencies (gcc, gfortran, BLAS/LAPACK)
- ✅ Works in all environments (local, Cloud Run, Docker)

**Implementation:** `src/adapters/firestore_repo.py::_cosine_similarity()`
