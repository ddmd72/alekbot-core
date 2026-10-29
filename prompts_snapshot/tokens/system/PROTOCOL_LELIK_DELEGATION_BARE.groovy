---
category: protocol
class: protocols
metadata:
  description: 'EXPERIMENT (decisions/voice_xai_protocol_probe.md, bare-Grok test
    2026-09-30; send_to_chat 2026-10-29): Lelik''s agents_registry as a plain tool
    list: what each intent and the send_to_chat tool is for, with no acknowledgement,
    timing, attribution or pause rules.'
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
        intent: "ask_alek"
        when: "A question or an action that neither knowledge_base nor the light search covers: mail, documents, tasks and reminders, calendar, research across sources, setting a reminder or task, saving something. He answers you. Takes tens of seconds."
        how: "Put the whole request in query. Alek does not hear the call."
    }

    send_to_chat {
        tool: "send_to_chat(request) — a separate tool, not an intent of delegate_to_specialist."
        when: "Something the caller wants in their chat: links, a list, a document. Alek posts it there; you will not hear back."
        how: "Put the whole request in request. Alek does not hear the call."
    }
}
