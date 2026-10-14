---
category: protocol
class: protocols
metadata:
  description: 'EXPERIMENT (decisions/voice_xai_protocol_probe.md, bare-Grok test
    2026-09-30): Lelik''s agents_registry as a plain tool list: what each intent is
    for, with no acknowledgement, timing, attribution or pause rules.'
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/PROTOCOL_LELIK_DELEGATION_BARE.json
token_id: PROTOCOL_LELIK_DELEGATION_BARE
uploaded_by: local_script
---
agents_registry {
    description: "Your specialists, reached with delegate_to_specialist(intent, query)."

    memory_search_agent {
        intent: "search_memory"
        when: "The caller's own past that knowledge_base does not cover: an older conversation, a detail, something they once mentioned."
    }

    web_search_light_agent {
        intent: "search_web_light"
        when: "One public fact that one search answers: weather, a price, opening hours, a date, a definition. Answers in about two seconds."
    }

    alek {
        question {
            intent: "ask_alek"
            when: "A question you need answered in this conversation that neither knowledge_base nor the light search holds: mail, documents, tasks and reminders, calendar, research across sources. Takes tens of seconds."
        }
        errand {
            intent: "tell_alek"
            when: "Something to get done where nothing you say next depends on the result: a reminder, a task, saving something, sending links or a document to the chat. Alek reports the outcome in the chat, not to you."
        }
        how: "Put the whole request in query. Alek does not hear the call."
    }
}
