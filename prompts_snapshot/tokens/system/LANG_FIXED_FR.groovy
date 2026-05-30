---
category: output_language
class: policies
metadata:
  created_at: '2026-03-23'
  description: Fixed French language policy
  override_by:
  - ACCOUNT
  - USER
  use_case: Users who want bot to always respond in French
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
source_file: firestore_utils/uploads/LANG_FIXED_FR.json
token_id: LANG_FIXED_FR
uploaded_by: local_script
---
@critical
rule Output_Language_Fixed_FR() {
    definition: "Fixed output language policy. All responses in French."
    instruction: "Always respond in French (français), regardless of what language the user writes in."
}
