---
category: conflict_resolution
class: conflict_resolution
metadata:
  description: RouterAgent v4 — conflict_resolution section
  override_by:
  - SYSTEM
  source: split from COGNITIVE_PROCESS_ROUTER v3
source_file: firestore_utils/uploads/ROUTER_CONFLICT_RESOLUTION.json
token_id: ROUTER_CONFLICT_RESOLUTION
uploaded_by: local_script
---
rule: "When a query could plausibly require deeper KB retrieval than a single search pass — set needs_memory_search=true and/or lower confidence below 0.75."
examples: [
    "Single fact query BUT data is mutable (blood pressure, weight) → needs_memory_search=true",
    "Ack after medical discussion ('Ок, дякую') — topic continuity unclear → confidence < 0.75"
]
