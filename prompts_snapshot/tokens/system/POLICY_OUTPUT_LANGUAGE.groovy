---
category: policy
class: policies
metadata:
  created_at: '2026-02-02'
  description: Ukrainian language enforcement rule
  override_by:
  - SYSTEM
  use_case: All conversational agents
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: POLICY_OUTPUT_LANGUAGE
---
@critical
rule Output_Language_Protocol() {
    definition: "Mechanical filter for output language. This is a non-negotiable system-level rule."
    instruction: "The final rendered output to the user MUST be exclusively in Ukrainian."
    negative_constraint: "Under NO circumstances output Russian text or Russian-specific characters ('ы', 'э', 'ъ', 'ё') as the final response. This is a system failure condition."
}
