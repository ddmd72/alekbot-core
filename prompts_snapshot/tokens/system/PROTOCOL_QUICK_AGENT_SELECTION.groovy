---
category: protocol
class: protocols
metadata:
  created_at: '2026-02-26'
  description: 'Agent selection registry for QuickResponseAgent — memory search +
    light web search (intent: search_web_light, single-pass only)'
  override_by:
  - SYSTEM
  - AGENT
  use_case: Agent selection and delegation for quick responses
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/PROTOCOL_QUICK_AGENT_SELECTION.json
token_id: PROTOCOL_QUICK_AGENT_SELECTION
uploaded_by: local_script
---
agents_registry {
    description: "Available specialist agents. Use delegate_to_specialist(intent, query) to call them."

    memory_search_agent {
        intent: "search_memory"
        when: "User asks anything that requires retrieving facts from the personal knowledge base."
        how: [
            "Describe the topic — what information is needed and which aspects to cover.",
            "If you know relevant specifics from biographical context (car brand, spouse name, condition name, etc.) — include them directly in the query. They become precision anchors for vector search.",
            "Avoid question framing — topic descriptions are denser and match KB fact vocabulary better."
        ]

        examples: [
            {
                user_query: "What car do I have?"
                context: "Bio mentions Toyota Corolla"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Car details: Toyota Corolla model, year, plate, insurance, service history")'
                note: "Known brand included — anchors recall across all car-related facts."
            },
            {
                user_query: "Remind me of my dietary restrictions"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Dietary restrictions: prohibited foods, allergies, intolerances, health conditions")'
            },
            {
                user_query: "tell me more"
                context: "Previous turn was about the user's work project"
                tool_call: 'delegate_to_specialist(intent="search_memory", query="Work project details: current status, goals, tasks, progress")'
                note: "Resolve vague follow-up from prior context — never pass bare 'tell me more'"
            }
        ]

        anti_patterns: [
            "❌ DON'T use question framing — describe the topic, not the question",
            "❌ DON'T omit known specifics (brand, name, condition) — they are precision anchors",
            "❌ DON'T use for external or real-time information"
        ]
    }

    web_search_agent {
        intent: "search_web_light"
        when: "User asks for external, current, or real-time information: news, prices, world facts, product specs, public events, weather."
        how: "Pass the user's question as query. Keep it natural and complete. Use the language the user wrote in."
        note: "Single call only — do not repeat or rephrase. One pass is sufficient."

        examples: [
            {
                user_query: "What is the euro exchange rate right now?"
                tool_call: 'delegate_to_specialist(intent="search_web_light", query="current euro exchange rates")'
            },
            {
                user_query: "Weather in Paris next week?"
                tool_call: 'delegate_to_specialist(intent="search_web_light", query="weather in Paris next week detailed forecast")'
            },
            {
                user_query: "What's happening in Valencia this weekend?"
                tool_call: 'delegate_to_specialist(intent="search_web_light", query="events in Valencia this weekend")'
            }
        ]

        anti_patterns: [
            "❌ DON'T use for questions about the user's personal data",
            "❌ DON'T call multiple times — one query covers it"
        ]
    }

    email_search_agent {
        intent: "search_emails"
        when: "User asks to find, search, or recall their emails by topic, sender, event, or document type."
        how: "Pass the user's question as query. Keep it natural. The agent extracts search terms and runs vector search against the indexed email archive."

        examples: [
            {
                user_query: "Find my flight booking confirmation for Paris"
                tool_call: 'delegate_to_specialist(intent="search_emails", query="find my flight booking confirmation for Paris")'
            },
            {
                user_query: "Any emails from the accountant about the invoice?"
                tool_call: 'delegate_to_specialist(intent="search_emails", query="emails from the accountant about the invoice")'
            },
            {
                user_query: "Did the clinic send me an appointment reminder?"
                tool_call: 'delegate_to_specialist(intent="search_emails", query="appointment reminder from the clinic")'
            }
        ]

        anti_patterns: [
            "❌ DON'T use for personal facts stored in the knowledge base (use search_memory instead)",
            "❌ DON'T use for real-time or external information (use search_web_light instead)",
            "❌ DON'T reformulate the query — pass the user's words naturally"
        ]
    }
}