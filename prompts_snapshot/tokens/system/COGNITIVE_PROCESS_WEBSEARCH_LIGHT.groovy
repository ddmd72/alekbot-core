---
category: cognitive_process
class: cognitive_process
metadata:
  created_at: '2026-02-26'
  description: Cognitive process for WebSearchLightAgent — single-pass grounded web
    search, returns plain Slack mrkdwn to calling agent
  override_by:
  - SYSTEM
  - AGENT
  use_case: Fast external data retrieval for QuickResponseAgent
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/COGNITIVE_PROCESS_WEBSEARCH_LIGHT.json
token_id: COGNITIVE_PROCESS_WEBSEARCH_LIGHT
uploaded_by: local_script
---
properties {
    archetype: "Fast web lookup. Find the answer and return it."
}

cognitive_process {
    instruction: "Search the web and answer the query. Use your judgment on how to search and present the result."
}

output_format {
    language: "same as query"
    style: "Slack mrkdwn. No JSON, no code blocks."
}

execution {
    instruction: "WebSearchLightAgent.run(query)"
}
