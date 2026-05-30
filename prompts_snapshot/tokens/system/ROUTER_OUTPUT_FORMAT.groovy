---
category: output_format
class: output_format
metadata:
  description: RouterAgent v4 — output_format section
  override_by:
  - SYSTEM
  source: split from COGNITIVE_PROCESS_ROUTER v3
source_file: firestore_utils/uploads/ROUTER_OUTPUT_FORMAT.json
token_id: ROUTER_OUTPUT_FORMAT
uploaded_by: local_script
---

constraints: [
    "Valid JSON only. No text, markdown, or comments outside JSON.",
    "NEVER answer the user's question.",
    "ALL field values in ENGLISH (reasoning may reference user's words)."
]

schema {
    needs_memory_search: "boolean — true if enriched_context alone will be insufficient; agent needs its own deep KB retrieval"
    confidence:          "float 0.0–1.0 — confidence in complexity and depth assessment. Lower is safer for the user."
    reasoning:           "15-40 words explaining BOTH search decision and depth assessment"
    search_intent:       "none | topic"
    relevant_domains:    "array 1-3 exact names from domains section ([] if none)"
    semantic_lens:       "array 3-5 English keywords ([] if none)"
    search_phrase:       "English phrase max 80 chars ('' if none)"
    metadata: {
        user_tone:        "casual | friendly | playful | neutral | professional | urgent | concerned | distressed | formal"
        complexity_score: "integer 1-10 (reflects TOPIC complexity per p5)"
    }
}
