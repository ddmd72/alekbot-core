---
category: anti_patterns
class: anti_patterns
metadata:
  description: MemorySearchAgent v4 — anti_patterns section
  override_by:
  - SYSTEM
  - AGENT
  source: split from COGNITIVE_PROCESS_MEMORY_SEARCH v3
source_file: firestore_utils/uploads/MEMORYSEARCH_ANTI_PATTERNS.json
token_id: MEMORYSEARCH_ANTI_PATTERNS
uploaded_by: local_script
---
anti_patterns: [
"❌ DON'T exceed 5 keywords",
"❌ DON'T reuse the same words across keywords, primary_query, and alternative_query",
"❌ DON'T make primary_query and alternative_query cover the same semantic angle",
"❌ DON'T use names or specifics you don't actually know from context",
"❌ DON'T answer the user's question — only produce search keys"
]
