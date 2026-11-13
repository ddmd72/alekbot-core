---
category: cognitive_process
class: cognitive_process
metadata:
  description: EmailSearchAgent — cognitive_process for LLM query extraction (MemorySearchAgent-like)
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/EMAILSEARCH_COGNITIVE_PROCESS.json
token_id: EMAILSEARCH_COGNITIVE_PROCESS
uploaded_by: local_script
---
instruction: "Follow these steps silently. This is your internal process — never output it."
steps: [
    "1. SUBJECT: Name the core subject of the search request — topic, event, document type, entity, or amount.",

    "2. PRIMARY: Compose a phrase describing what the indexer's extracted fact would say about this subject — the captured event or information, not the email framing. Example: for a flight booking, think 'flight booked to Berlin departure date airline', not 'email about booking'.",

    "3. ALTERNATIVE: Choose an orthogonal angle the indexer might have used — a different aspect of the same event (counterparty, amount, reference number, outcome). Zero verbatim overlap with primary.",

    "4. TAGS: Pick 3–5 short English terms (1–2 words each) that an indexer would assign as category tags for this type of fact. Must not repeat words already in primary_query or alternative_query. Minimum 3, maximum 5.",

    "5. DATE RANGE: If the request contains a time signal — a year, month, quarter, season, or relative expression ('last 3 months', 'in 2023', 'since January', 'this year') — derive the exact calendar interval and set date_from and date_to as YYYY-MM-DD strings. Use current_date_time to resolve relative expressions. If no time signal → set both to null.",

    "6. EXACT KEYS: The index is searched by meaning, and a bare identifier carries none, so the search also matches identifiers literally across the whole mailbox, at any date. List each identifier the request gives — an order, invoice, case, ticket, tracking, account or reference number or code, or an email address — copied exactly as written: never translate, reformat or complete it. Never put a topic word, a name or a phrase here: a literal match on a common word floods the result with unrelated mail. None → an empty list. At most 3.",

    "7. OUTPUT: Emit valid JSON with exactly six fields: primary_query, alternative_query, tags, date_from, date_to, exact_keys. All string values in ENGLISH except exact_keys, which keep the identifiers as written. Nothing before { or after }."
]
