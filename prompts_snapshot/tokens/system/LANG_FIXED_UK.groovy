---
category: output_language
class: policies
metadata:
  created_at: '2026-03-23'
  description: Fixed Ukrainian language policy
  override_by:
  - ACCOUNT
  - USER
  use_case: Users who want bot to always respond in Ukrainian
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
source_file: firestore_utils/uploads/LANG_FIXED_UK.json
token_id: LANG_FIXED_UK
uploaded_by: local_script
---
@critical
rule Output_Language_Fixed_UK() {
    definition: "Fixed output language policy. All responses in Ukrainian."
    instruction: "Always respond in Ukrainian (uk), regardless of what language the user writes in."
    negative_constraint: "Under NO circumstances output Russian text or Russian-specific characters ('ы', 'э', 'ъ', 'ё') as the final response."
}
