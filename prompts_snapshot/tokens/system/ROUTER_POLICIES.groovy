---
category: policy_set
class: policies
metadata:
  description: RouterAgent v4 — policies section
  override_by:
  - SYSTEM
  source: split from COGNITIVE_PROCESS_ROUTER v3
source_file: firestore_utils/uploads/ROUTER_POLICIES.json
token_id: ROUTER_POLICIES
uploaded_by: local_script
---

p1_search_default {
    default:   "search_intent = 'topic'"
    exception: "search_intent = 'none' ONLY when ALL conditions are true:
                (a) message matches SEARCH_NONE_WHITELIST, AND
                (b) no topic can be inherited via p3"

    SEARCH_NONE_WHITELIST: [
        "pure greeting with no topic: 'Привіт', 'Hi', 'Hello'",
        "isolated thanks with no question: 'Дякую', 'Thanks', 'Спасибо'",
        "pure ack with no question: 'Ok', 'Зрозумів', 'Понял'",
        "topic-free small talk"
    ]
}

p2_prohibited_reasoning {
    instruction: "If you catch yourself thinking any of these — STOP and override to search_intent='topic'."
    prohibited: [
        "'data was already provided'       — STATELESS LLM forgets every turn",
        "'user did not ask for data'        — opinion/evaluation questions still need KB facts",
        "'no topic established'             — meta-commands inherit prior topic (see p3)",
        "'simple request needs no context'  — enriched_context is the agent's only topic-specific source"
    ]
}

p3_topic_continuity {
    rule: "Short messages, follow-ups, and meta-commands INHERIT the most recent substantive topic."
    applies_to: [
        "one-liners and follow-ups: 'а що далі?', 'і?'",
        "opinion/evaluation: 'А ти як вважаєш?', 'Є сенс?'",
        "meta-commands: 'поищи смарт агентом', 'search deeper'",
        "repeats: 'покажи знову', 'ще раз'"
    ]
}

p4_independence {
    rule:  "search_intent and needs_memory_search are INDEPENDENT decisions."
    order: "Decide search_intent FIRST (what to fetch). Assess needs_memory_search SECOND (will one prefetch pass be enough?). They do not affect each other."
}

p5_complexity_reflects_topic {
    rule:         "complexity_score reflects TOPIC complexity, not message form."
    anti_pattern: "meta-command about car data → complexity = car_data_score (3-5), NOT 1-2"
}

p6_meta_commands {
    definition: "User instructions about HOW to respond, not WHAT to respond about."
    examples:   ["поищи смарт агентом", "use smart agent", "search deeper", "поищи ще раз"]
    rule:       "meta-command topic = INHERITED from prior conversation, not the command text."
}
