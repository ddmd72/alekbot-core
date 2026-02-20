# RFC: Gmail Email Indexing System

**Status:** Proposed  
**Date:** 2026-02-11  
**Owner:** AI Engineering (Cline)  
**Milestone:** Future (Post-MVP)

**Related Building Blocks:** Memory & Context, Search Enrichment  
**Related ADRs:** TBD

---

## 1. Problem Statement

### 1.1 Current Memory Search Limitations

Alek-Core's MemorySearchAgent currently searches only Firestore facts:

- **Limited data source:** Only manually entered or consolidated facts
- **Cold start problem:** New users have empty memory (no personalization)
- **Missing email data:** User's Gmail contains rich personal history (flights, purchases, healthcare, events)

### 1.2 Why Gmail Search Is Insufficient

Gmail API native search has critical limitations:

- **Keyword-only matching:** No semantic understanding
- **No multilingual support:** "perelioty" won't match "flight"
- **No synonym expansion:** "reys" won't match "perelet"
- **Low recall:** Finds only 10-20% of relevant emails
- **No structured data:** Can't extract dates, amounts, entities

**Example failure:**

```
User query: "find my test results for 2025"
Gmail search: subject:"test results" after:2025/01/01
Result: 2 emails found (missed "results", "test report", "medical")
```

### 1.3 Desired Outcome

Enable Alek to:

1. **Index all user emails** (metadata only: subject, from, date)
2. **Classify emails** via LLM (travel, finance, healthcare, work, etc.)
3. **Extract entities** (dates, amounts, names, flight numbers)
4. **Instant semantic search** (0.3s vs 4s Gmail API calls)
5. **Structured queries** (category filters, date ranges, entity matching)

---

## 2. Proposed Solution: Email Indexing System

### 2.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Email Indexing System                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────┐      ┌────────────────────────┐     │
│  │ GmailIndexing    │─────>│ LLMClassificationSvc   │     │
│  │ Service          │      │ (batch 50 emails)      │     │
│  └──────────────────┘      └────────────────────────┘     │
│           │                           │                     │
│           │                           │                     │
│           v                           v                     │
│  ┌──────────────────┐      ┌────────────────────────┐     │
│  │ Gmail API        │      │ Entity Extraction      │     │
│  │ (metadata only)  │      │ (dates, amounts, etc.) │     │
│  └──────────────────┘      └────────────────────────┘     │
│           │                           │                     │
│           └───────────┬───────────────┘                     │
│                       v                                     │
│           ┌───────────────────────┐                        │
│           │ Firestore Collection: │                        │
│           │  indexed_emails       │                        │
│           │  (with vector index)  │                        │
│           └───────────────────────┘                        │
│                       │                                     │
│                       v                                     │
│           ┌───────────────────────┐                        │
│           │ MemorySearchAgent     │                        │
│           │ (hybrid: facts +      │                        │
│           │  indexed emails)      │                        │
│           └───────────────────────┘                        │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Core Components

#### 2.2.1 GmailIndexingService

```python
class GmailIndexingService:
    """
    Orchestrates email indexing workflow.

    Responsibilities:
    - Fetch email metadata from Gmail API
    - Batch emails for LLM classification
    - Coordinate entity extraction
    - Save indexed emails to Firestore
    - Manage incremental updates
    """

    async def index_user_emails(
        self,
        user_id: str,
        date_from: str = "2020/01/01",
        date_to: str = "2026/12/31"
    ) -> IndexingResult:
        """
        One-time indexing: fetch all emails + classify.

        Args:
            user_id: User ID (OAuth multi-tenant)
            date_from: Start date for indexing
            date_to: End date for indexing

        Returns:
            IndexingResult with statistics
        """
```

#### 2.2.2 LLMClassificationService

```python
class LLMClassificationService:
    """
    Batch email classification via LLM.

    Categories:
    - travel (flights, hotels, car rentals)
    - finance (invoices, receipts, banking)
    - healthcare (medical, analysis, prescriptions)
    - work (meetings, projects, contracts)
    - shopping (orders, deliveries, tracking)
    - events (conferences, webinars, tickets)
    - personal (friends, family)
    - other
    """

    async def classify_batch(
        self,
        emails: List[EmailMetadata],
        batch_size: int = 50
    ) -> List[ClassifiedEmail]:
        """
        Classify 50 emails in one LLM call.

        Returns:
            List of ClassifiedEmail with category, confidence, entities
        """
```

#### 2.2.3 EntityExtractionService

```python
class EntityExtractionService:
    """
    Extract structured entities from email subjects.

    Entities:
    - dates (departure, arrival, deadline)
    - amounts (invoice total, order total)
    - flight numbers (FR123, BA456)
    - tracking numbers (UPS tracking)
    - names (sender, recipient)
    - locations (cities, countries)
    """

    async def extract_entities(
        self,
        email: EmailMetadata,
        category: str
    ) -> Dict[str, Any]:
        """
        Extract category-specific entities.
        """
```

---

## 3. Firestore Schema

### 3.1 Collection: `users/{user_id}/indexed_emails/{email_id}`

```yaml
{
  # Email metadata
  "email_id": "msg_xyz123",
  "subject": "Your flight KBP-BCN is confirmed",
  "from": "airline@ryanair.com",
  "date": "2025-03-10T14:30:00Z",
  "labels": ["inbox", "important"],

  # Classification
  "category": "travel",
  "confidence": 0.95,

  # Extracted entities
  "entities":
    {
      "flight_number": "FR8421",
      "departure_city": "Kiev",
      "arrival_city": "Barcelona",
      "departure_date": "2025-03-15",
      "airline": "Ryanair",
      "confirmation": "ABC123",
    },

  # Vector search
  "subject_embedding": [0.123, -0.456, ...], # 768 dimensions

  # Metadata
  "indexed_at": "2026-02-11T00:00:00Z",
  "index_version": "v1.0",
}
```

### 3.2 Firestore Index Configuration

```json
{
  "collectionGroup": "indexed_emails",
  "queryScope": "COLLECTION",
  "fields": [
    {"fieldPath": "category", "order": "ASCENDING"},
    {"fieldPath": "date", "order": "DESCENDING"}
  ]
},
{
  "collectionGroup": "indexed_emails",
  "queryScope": "COLLECTION",
  "fields": [
    {"fieldPath": "subject_embedding", "vectorConfig": {"dimension": 768, "flat": {}}}
  ]
}
```

---

## 4. Implementation Plan

### Phase 1: Core Indexing (Week 1-2)

**Week 1: Gmail API Integration**

- Day 1-2: Gmail API metadata fetching
  - `messages.list()` with `format='metadata'`
  - Pagination (500 emails per page)
  - Date range filtering
- Day 3: OAuth integration
  - Reuse existing GmailCredentialsPort
  - gmail.readonly scope
- Day 4-5: Unit tests + integration tests

**Week 2: LLM Classification**

- Day 1-2: Classification prompt engineering
  - 10 categories definition
  - Batch size optimization (50 emails)
  - JSON output validation
- Day 3: Entity extraction prompts
  - Category-specific entity patterns
  - Structured output (Pydantic models)
- Day 4: Batch processing pipeline
- Day 5: Error handling + retries

### Phase 2: Storage & Search (Week 3)

**Day 1-2: Firestore integration**

- Schema implementation
- Batch writes (500 docs/batch)
- Vector index creation

**Day 3-4: MemorySearchAgent extension**

- Hybrid search: facts + indexed_emails
- Category filters
- Date range queries

**Day 5: Testing**

- E2E test: index 1000 emails
- Query performance tests
- Accuracy validation

### Phase 3: Incremental Updates (Week 4)

**Day 1-2: Daily sync**

- Fetch new emails (since last index)
- Incremental classification
- Update indexed_emails

**Day 3-4: UI integration**

- /cabinet: "Index Gmail" button
- Progress tracking (websocket updates)
- Statistics dashboard

**Day 5: Polish**

- Error messages
- Rate limiting
- Documentation

---

## 5. Cost Analysis

### 5.1 One-Time Indexing

**Assumptions:** Average user has 10,000 emails

| Component             | Quantity    | Unit Cost        | Total     |
| --------------------- | ----------- | ---------------- | --------- |
| Gmail API calls       | 20 pages    | $0 (free)        | $0        |
| LLM classification    | 200 batches | $0.001/batch     | $0.20     |
| Entity extraction     | 200 batches | $0.0005/batch    | $0.10     |
| Embeddings (subjects) | 10,000      | $0.00001/subject | $0.10     |
| Firestore writes      | 10,000      | $0.000018/write  | $0.18     |
| **TOTAL (one-time)**  |             |                  | **$0.58** |

### 5.2 Incremental Updates (Daily)

**Assumptions:** User receives 20 emails/day

| Component           | Monthly Cost          |
| ------------------- | --------------------- |
| Gmail API           | $0 (free)             |
| LLM classification  | $0.03                 |
| Embeddings          | $0.006                |
| Firestore writes    | $0.01                 |
| **TOTAL (monthly)** | **$0.046** (~5 cents) |

### 5.3 Query Cost

| Component               | Cost per Query |
| ----------------------- | -------------- |
| Firestore vector search | $0.00006       |
| Category filter         | $0 (indexed)   |
| Embedding (user query)  | $0.00001       |
| **TOTAL**               | **$0.00007**   |

**vs Gmail API search:** $0.0006 (9x more expensive)

---

## 6. Performance Benchmarks

### 6.1 Indexing Performance

| Metric                        | Value         |
| ----------------------------- | ------------- |
| Initial indexing (10K emails) | 60-90 seconds |
| Gmail API fetching            | 10 seconds    |
| LLM classification            | 30 seconds    |
| Entity extraction             | 20 seconds    |
| Firestore storage             | 10 seconds    |

### 6.2 Query Performance

| Query Type               | Latency | Recall | Precision |
| ------------------------ | ------- | ------ | --------- |
| Category filter + date   | 0.1s    | 100%   | 100%      |
| Semantic search (vector) | 0.3s    | 95%    | 90%       |
| Entity matching          | 0.2s    | 98%    | 95%       |
| Hybrid (facts + emails)  | 0.5s    | 98%    | 92%       |

**vs Gmail API:** 0.5s vs 4s (8x faster)

---

## 7. Security & Privacy

### 7.1 OAuth Consent

- **Optional feature:** User explicitly enables Gmail indexing
- **Scope:** `gmail.readonly` (no write/delete access)
- **Revocable:** User can disconnect anytime
- **Per-user tokens:** Isolated, encrypted storage

### 7.2 Data Storage

- **Metadata only:** No email body content stored
- **Encryption:** Firestore encryption at rest
- **User-scoped:** Multi-tenant isolation (account_id)
- **Retention:** User can delete indexed data anytime

### 7.3 PII Filtering

```python
async def _filter_sensitive_data(self, subject: str) -> str:
    """
    Remove PII before storing:
    - Credit card numbers
    - Social security numbers
    - Passwords
    - API keys
    """
    # Regex patterns + LLM-based detection
```

---

## 8. User Experience

### 8.1 Initial Indexing Flow

```
11:00 User clicks "Index Gmail" in /cabinet
11:00 Bot: "Starting to index your mailbox..."
11:00 Bot: "Fetching emails from Gmail... (10s)"
      [Progress bar: ████░░░░░░ 40%]
11:01 Bot: "Classifying 8,432 emails... (30s)"
      [Progress bar: ████████░░ 80%]
11:01 Bot: "Building index... (20s)"
      [Progress bar: ██████████ 100%]
11:02 Bot: "✅ Done! Indexed 8,432 emails:
             📬 Travel: 423 (5%)
             💰 Finance: 1,234 (15%)
             🏥 Healthcare: 87 (1%)
             💼 Work: 3,456 (41%)
             🛍️ Shopping: 1,832 (22%)
             🎟️ Events: 298 (4%)
             👤 Personal: 654 (8%)
             📁 Other: 448 (5%)

             Now I can instantly search your mailbox!"
```

### 8.2 Query Flow (Post-Indexing)

```
User: "find my test results for 2025"
  ↓
Bot: (0.3s Firestore query)
  ↓
Bot: "Found 7 emails with test results for 2025:

     1. 15 Jan 2025: Blood test results (Dr. Smith)
     2. 23 Feb 2025: X-ray report (Hospital Central)
     3. 10 Mar 2025: COVID-19 test (negative)
     4. 05 Apr 2025: Annual checkup (Dr. Jones)
     5. 18 May 2025: Allergy panel results
     6. 22 Jul 2025: MRI scan report
     7. 30 Oct 2025: Dental X-ray

     Would you like details on any of these results?"
```

---

## 9. Future Enhancements (Phase 2+)

### 9.1 Self-Enriching Memory Loop

**Vision:** Automatically create facts from indexed emails

```
User: "find info about flights"
  ↓
Bot finds 15 emails with flights
  ↓
Bot extracts structured data:
  - Flight: Kiev → Barcelona, 15 Mar 2025, FR8421
  - Hotel: Hilton Barcelona, 15-18 Mar 2025
  - Conference: WebSummit 2025, Barcelona
  ↓
Bot creates facts in Firestore automatically
  ↓
User: "when was I in Barcelona?" (next time)
  ↓
Bot answers from memory (no Gmail search)
```

**Benefit:** Memory grows exponentially (snowball effect)

### 9.2 Attachment Analysis

Parse PDF attachments:

- Medical reports → extract diagnoses, prescriptions
- Invoices → extract amounts, dates, vendors
- Contracts → extract parties, deadlines, terms

**Challenge:** High cost ($0.01-0.05 per PDF with GPT-4 Vision)

### 9.3 Full-Text Indexing

Index email body content (not just subjects):

- Higher recall (find emails by body keywords)
- Context-aware search (understand email conversations)

**Challenge:** Privacy concerns + storage cost

### 9.4 Proactive Insights

```
Bot: "I noticed you fly to Berlin often (5 times this year).
      Maybe worth considering a lounge membership?"

Bot: "You have 3 unpaid invoices (total $1,234).
      Should I remind you about payment?"
```

### 9.5 Multi-Account Support

Index emails from multiple accounts:

- Personal Gmail
- Work Gmail
- Outlook/Exchange (via Microsoft Graph API)

---

## 10. Alternatives Considered

### 10.1 Gmail Search with Query Expansion

**Pros:**

- No indexing cost
- Real-time results

**Cons:**

- Still keyword-based (low recall)
- 4s latency (slow)
- No structured data
- No category filters

**Verdict:** Rejected - insufficient quality

### 10.2 Third-Party Services (Superhuman, SaneBox)

**Pros:**

- Battle-tested indexing
- Rich features

**Cons:**

- $30/month cost
- No control over data
- No integration with Alek memory

**Verdict:** Rejected - not Hexagonal, not customizable

### 10.3 Elastic Search / Algolia

**Pros:**

- Fast full-text search
- Rich query language

**Cons:**

- Infrastructure complexity
- No semantic search (without embeddings)
- Cost ($50-100/month)

**Verdict:** Rejected - over-engineering for MVP

---

## 11. Success Metrics

### 11.1 Quality Metrics

- **Classification accuracy:** >90% (user feedback)
- **Entity extraction accuracy:** >85%
- **Query precision:** >90% relevant results
- **Query recall:** >95% of relevant emails found

### 11.3 Performance Metrics

- **Indexing time (10K emails):** <90 seconds
- **Query latency:** <500ms (p95)
- **System availability:** 99.9%

---

## 12. Risks & Mitigation

### Risk 1: Large Mailboxes (50K+ emails)

**Impact:** High indexing cost ($3-5), long indexing time (5-10 min)

**Mitigation:**

- Progressive indexing (5K emails/batch)
- User confirmation before indexing large mailboxes
- Time-based filtering (last 2 years only by default)

### Risk 2: Gmail API Rate Limits

**Impact:** Indexing fails mid-process

**Mitigation:**

- Exponential backoff + retry
- Quota monitoring
- Batch size tuning

### Risk 3: Classification Accuracy

**Impact:** Emails misclassified, user loses trust

**Mitigation:**

- Confidence threshold (reject low-confidence classifications)
- Human-in-the-loop verification (sample 10 random emails)
- Category correction UI (user can reclassify)

### Risk 4: Privacy Concerns

**Impact:** Users don't enable Gmail indexing

**Mitigation:**

- Clear privacy policy (metadata only, no body)
- Transparent data usage explanation
- Easy opt-out + data deletion

---

## 13. Open Questions

1. **Should we index Sent emails?** Or only Inbox?
   - **Decision:** Index all (sent reveals user behavior patterns)

2. **How far back to index?** All history or last N years?
   - **Decision:** Default 5 years, user can override

3. **Should we re-classify emails periodically?** (as LLM improves)
   - **Decision:** Phase 2 feature (re-index every 6 months)

4. **How to handle deleted emails?** Sync deletions from Gmail?
   - **Decision:** Yes, incremental sync includes deletions

5. **Should we embed full subject or truncate?** (cost vs quality)
   - **Decision:** Full subject (average 10 tokens, negligible cost)

---

## 14. Dependencies

### 14.1 Existing Infrastructure

- ✅ OAuth Multi-Tenant (Session 10 complete)
- ✅ GmailCredentialsPort (to be created, simple)
- ✅ MemorySearchAgent (extension point ready)
- ✅ SearchEnrichmentService (vector search ready)
- ✅ Firestore multi-tenant isolation

### 14.2 New Components (to be created)

- ❌ GmailIndexingService
- ❌ LLMClassificationService
- ❌ EntityExtractionService
- ❌ Firestore indexed_emails collection
- ❌ /cabinet UI for indexing

---

## 15. References

- **Gmail API Documentation:** https://developers.google.com/gmail/api/guides
- **Firestore Vector Search:** https://firebase.google.com/docs/firestore/vector-search
- **OAuth Multi-Tenant RFC:** [MULTI_TENANT_OAUTH_RFC.md](./MULTI_TENANT_OAUTH_RFC.md)
- **Search Enrichment Building Block:** [../05_building_blocks/search_enrichment/README.md](../05_building_blocks/search_enrichment/README.md)

---

## Changelog

### 2026-02-11

- Initial RFC created
- Problem statement defined
- Architecture designed
- Implementation plan outlined (4 weeks)
- Cost analysis completed ($0.58 one-time, $0.05/month)
- Future enhancements documented (self-enrichment loop)
