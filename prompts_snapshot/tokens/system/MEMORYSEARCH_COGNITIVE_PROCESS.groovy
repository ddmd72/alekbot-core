---
category: cognitive_process
class: cognitive_process
metadata:
  description: MemorySearchAgent v4 — cognitive_process section
  override_by:
  - SYSTEM
  - AGENT
  source: split from COGNITIVE_PROCESS_MEMORY_SEARCH v3
source_file: firestore_utils/uploads/MEMORYSEARCH_COGNITIVE_PROCESS.json
token_id: MEMORYSEARCH_COGNITIVE_PROCESS
uploaded_by: local_script
---

step_1_SUBJECT {
    → "What personal data is the user looking for? Name the core subject."
}

step_2_KEYWORDS {
    → "Pick 3–5 short English terms (1–2 words) that best tag the subject."
    → "Hard limit 5. Must not overlap with queries."
}

step_3_QUERIES {
    → "PRIMARY: phrase describing what the KB fact itself would say — no framing words like 'user' or 'my'."
    → "ALTERNATIVE: rephrase using synonyms or a completely different angle."
    → "PRIMARY and ALTERNATIVE must cover different semantic neighborhoods — zero verbatim overlap."
}

step_4_DOMAINS {
    → "Map subject to 1–2 domains from the schema enum."
    → "Always include at least one."
}

step_5_OUTPUT {
    → "Emit valid JSON. No text outside JSON."
}
