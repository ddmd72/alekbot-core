---
category: knowledge_base
class: knowledge_base
metadata:
  description: RouterAgent v4 — knowledge_base section
  override_by:
  - SYSTEM
  source: split from COGNITIVE_PROCESS_ROUTER v3
source_file: firestore_utils/uploads/ROUTER_KNOWLEDGE_BASE.json
token_id: ROUTER_KNOWLEDGE_BASE
uploaded_by: local_script
---
data_architecture {
    stateless_model {
        fact:        "Responding agent is a STATELESS LLM — no memory of previous responses."
        implication: "KB facts from prior responses are GONE next turn. YOU are the only KB→agent path."
    }

    context_sources {
        A_biographical_baseline: "Core user facts, auto-included every turn."
        B_enriched_context:      "KB facts retrieved via search channels. Without this, agent has [A]+[C] only."
        C_conversation_history:  "Dialogue TEXT only, NOT KB data."
    }

    enriched_context_limits {
        fact:        "Enriched context is ONE prefetched search pass — covers one semantic direction."
        implication: "Queries that span multiple domains or require cross-referencing facts NEED the agent to run its own memory_search."
    }
}

domains {
    biographical:    "Immutable identity (birthdate, blood type, citizenship)"
    possession:      "Owned objects (car, house, clothing, gadgets)"
    health:          "Conditions, biometrics (weight, allergies, symptoms)"
    medical_records: "Clinical data (lab results, prescriptions, diagnoses)"
    location:        "Addresses, residence, travel"
    work:            "Occupation, career (job title, company)"
    network:         "Contacts, relationships (family, friends)"
    preference:      "Habits, likes, dislikes, values, routines"
    skill:           "Abilities, knowledge, languages"
    project:         "Active projects, experiments"
    finance:         "Income, expenses, investments"
    education:       "Degrees, courses, learning"
    legal:           "Contracts, licenses, legal issues"
    entertainment:   "Leisure, hobbies, media"
    communication:   "Contact info, social media"
}
