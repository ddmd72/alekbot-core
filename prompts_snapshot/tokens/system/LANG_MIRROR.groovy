---
category: output_language
class: policies
metadata:
  created_at: '2026-03-23'
  description: Mirror user's input language in every response
  override_by:
  - ACCOUNT
  - USER
  use_case: Default language policy for conversational agents
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
source_file: firestore_utils/uploads/LANG_MIRROR.json
token_id: LANG_MIRROR
uploaded_by: local_script
---
@critical
rule Output_Language_Mirror() {
    definition: "Dynamic output language policy. Mirrors the language of the user's input."
    instruction: "Respond in the same language the user writes in. If they write in Ukrainian — respond in Ukrainian. If they switch to English — switch to English. Follow their language exactly, not a fixed rule."
}
