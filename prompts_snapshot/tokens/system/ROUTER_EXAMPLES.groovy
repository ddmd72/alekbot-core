---
category: examples
class: examples
metadata:
  description: RouterAgent v4 — examples section
  override_by:
  - SYSTEM
  source: split from COGNITIVE_PROCESS_ROUTER v3
source_file: firestore_utils/uploads/ROUTER_EXAMPLES.json
token_id: ROUTER_EXAMPLES
uploaded_by: local_script
---

ex_greeting {
    input:  "Hi!"
    output: '{"needs_memory_search":false,"confidence":0.95,"reasoning":"Greeting with no topic — whitelist match, no enrichment needed, single-turn chitchat","search_intent":"none","relevant_domains":[],"semantic_lens":[],"search_phrase":"","metadata":{"user_tone":"friendly","complexity_score":1}}'
}

ex_single_fact {
    context: "Single fact: only 1 fact for 1 subject. If user has only 1 car, 'What\'s my car\'s plate number?' is single fact. If user has 2 cars — it becomes multi-fact (ambiguous → needs_memory_search=true)."
    input:  "What's my car's plate number?"
    output: '{"needs_memory_search":false,"confidence":0.85,"reasoning":"Single possession fact — enriched_context from one search pass will cover vehicle registration","search_intent":"topic","relevant_domains":["possession"],"semantic_lens":["car","vehicle","plate","registration","insurance"],"search_phrase":"user vehicle registration plate number insurance details","metadata":{"user_tone":"casual","complexity_score":3}}'
}

ex_multiple_facts {
    input:  "What are my document numbers?"
    output: '{"needs_memory_search":true,"confidence":0.9,"reasoning":"Multiple document numbers — passport, ID, license span legal and biographical domains, exceeds single prefetch","search_intent":"topic","relevant_domains":["legal","biographical"],"semantic_lens":["passport","ID","license","document","number"],"search_phrase":"user document numbers passport ID driver license registration","metadata":{"user_tone":"casual","complexity_score":6}}'
}

ex_topic_continuation {
    context: "Prior exchange about lab test results"
    input:   "What about the results?"
    output:  '{"needs_memory_search":true,"confidence":0.8,"reasoning":"Short follow-up inherits lab topic — test results likely span multiple records, single prefetch insufficient","search_intent":"topic","relevant_domains":["medical_records","health"],"semantic_lens":["lab","test","results","lab results"],"search_phrase":"lab test results health metrics","metadata":{"user_tone":"casual","complexity_score":4}}'
}

ex_opinion_on_topic {
    context: "Prior exchange about Lisbon trip"
    input:   "What do you think? Is it worth it?"
    output:  '{"needs_memory_search":false,"confidence":0.85,"reasoning":"Opinion inherits Lisbon travel topic — enriched_context with travel preferences covers personalized answer","search_intent":"topic","relevant_domains":["location","preference"],"semantic_lens":["Lisbon","trip","travel","experience","preference"],"search_phrase":"Lisbon trip experience travel preferences impressions","metadata":{"user_tone":"casual","complexity_score":3}}'
}

ex_meta_command {
    context: "Prior exchange delivered lab data"
    input:   "search with the smart agent"
    output:  '{"needs_memory_search":true,"confidence":0.85,"reasoning":"Meta-command inherits lab topic — user explicitly requests deeper search, one prefetch insufficient","search_intent":"topic","relevant_domains":["medical_records","health"],"semantic_lens":["lab results","test results","health","biometrics"],"search_phrase":"lab test results health metrics biometrics","metadata":{"user_tone":"casual","complexity_score":5}}'
}

ex_complex_multi_domain {
    input:  "Show my blood test results and plan a diet"
    output: '{"needs_memory_search":true,"confidence":0.95,"reasoning":"Multi-step task across medical and dietary domains — deep retrieval required, single prefetch insufficient","search_intent":"topic","relevant_domains":["medical_records","health","preference"],"semantic_lens":["blood test","diet","restriction","allergy","nutrition"],"search_phrase":"blood test results dietary restrictions health conditions","metadata":{"user_tone":"neutral","complexity_score":8}}'
}

ex_travel_planning {
    input:  "Plan a weekend trip to Lisbon"
    output: '{"needs_memory_search":true,"confidence":0.9,"reasoning":"Multi-step planning across location, preference, possession — personalized logistics requires deep KB retrieval","search_intent":"topic","relevant_domains":["location","preference","possession"],"semantic_lens":["travel","Lisbon","flight","hotel","logistics"],"search_phrase":"travel plans trips logistics flights preferences transportation","metadata":{"user_tone":"neutral","complexity_score":7}}'
}
