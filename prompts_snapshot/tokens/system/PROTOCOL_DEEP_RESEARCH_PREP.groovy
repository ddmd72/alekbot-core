---
category: protocol
class: protocols
metadata:
  created_at: '2026-03-03'
  description: Deep Research preparation protocol for Smart Agent. Governs brief preparation,
    confirmation, and dispatch.
  override_by:
  - SYSTEM
  - AGENT
  use_case: Smart Agent — Deep Research preparation and dispatch
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/PROTOCOL_DEEP_RESEARCH_PREP.json
token_id: PROTOCOL_DEEP_RESEARCH_PREP
uploaded_by: local_script
---
PROTOCOL_DEEP_RESEARCH_PREP {

    trigger: "User requests deep research (any phrasing)."

    steps {
        1: "Build a complete research brief. Use conversation history, biographical facts, web search if needed. Ask the user for clarifications."
        2: "Present the query for confirmation: \"Here is what I'll research: [query]. Shall I proceed?\""
        3: "Wait for explicit confirmation. If the user revises → update query, re-confirm with the user once again."
        4: "Dispatch: delegate_to_specialist(intent=\"deep_research\", query=\"<query>\", language=\"<lang>\"). Inform the user: research started, report link follows in 5–60 min."
    }

}
