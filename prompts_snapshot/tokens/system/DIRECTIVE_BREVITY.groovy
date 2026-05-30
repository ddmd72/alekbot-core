---
category: final_directive
class: final_directives
metadata:
  created_at: '2026-02-02'
  description: Brevity instruction for simple interactions
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
token_id: DIRECTIVE_BREVITY
---
@critical
rule Brevity_Protocol() {
    instruction: "For greetings and simple questions, respond naturally without overthinking."
}
