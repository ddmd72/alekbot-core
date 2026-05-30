---
category: protocol
class: protocols
metadata:
  created_at: '2026-02-02'
  description: Memory search protocol for personal data retrieval
  override_by:
  - SYSTEM
  - AGENT
  use_case: Smart/Quick agents with memory access
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: PROTOCOL_SEARCH_MEMORY
---
agents_registry {
    description: "Available specialist agents. Use delegate_to_specialist(intent, query) to call them."

    web_search_agent {
        intent: "search_web"
        when: "User asks for external, current, or real-time information: news, prices, world facts, product specs, public events, documentation."
        how: "Pass the user's question as query. Keep it natural and complete. Use the language the user wrote in."

        examples: [
            {
                user_query: "Какой сейчас курс евро?"
                tool_call: 'delegate_to_specialist(intent="search_web", query="текущий курс евро к доллару и гривне")'
            },
            {
                user_query: "Что нового в Claude 4?"
                tool_call: 'delegate_to_specialist(intent="search_web", query="Claude 4 new features latest updates")'
            },
            {
                user_query: "Погода в Киеве завтра?"
                tool_call: 'delegate_to_specialist(intent="search_web", query="погода в Киеве завтра")'
            }
        ]

        anti_patterns: [
            "❌ DON'T use for questions about the user's personal data — use biographical_context instead",
            "❌ DON'T delegate simple conversational or opinion questions",
            "❌ DON'T change the language of the query unnecessarily"
        ]
    }
}
