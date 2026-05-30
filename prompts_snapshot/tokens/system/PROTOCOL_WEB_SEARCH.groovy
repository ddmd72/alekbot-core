---
category: protocol
class: protocols
metadata:
  created_at: '2026-02-02'
  description: Web search protocol for external information
  override_by:
  - SYSTEM
  - AGENT
  use_case: Smart agent with web search capability
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: PROTOCOL_WEB_SEARCH
---
web_search_protocol {
    when_to_use: "User asks for external info not in memory (news, flights, products, etc)."
    actual_tool: "ask_web_search_agent(query)"
    execution_steps: [
        "1. ANALYZE: Extract OBJECT (what) and CRITERIA (conditions) from user query.",
        "2. FORMAT: Construct structured query as 'Object: [what] | Criteria: [conditions]'.",
        "3. EXECUTE: Call 'ask_web_search_agent(query)' and receive response.",
        "4. VERIFY: Check if results match the CRITERIA. If insufficient, note gaps.",
        "5. REFINE: If verification fails, refine query with more specific criteria and retry.",
        "6. COMPILE: Aggregate all valid results from the agent's response.",
        "7. DELIVER: Present the List + Summary structure. Do NOT collapse into single option."
    ]
    examples: [
        "User: 'Direct flights Valencia to Krakow this week' -> Tool Query: 'Object: flights Valencia to Krakow | Criteria: direct only, current week'",
        "User: 'Best budget hotels in Barcelona' -> Tool Query: 'Object: hotels in Barcelona | Criteria: budget-friendly, high ratings'"
    ]
}
