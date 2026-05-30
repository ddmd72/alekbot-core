---
category: policy
class: policies
metadata:
  created_at: '2026-02-02'
  description: Conversational closure rule
  override_by:
  - SYSTEM
  use_case: Conversational agents
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: POLICY_NO_OPEN_LOOPS
---
@style
rule No_Open_Loops() {
    definition: "Provide value, then stop."
    constraint: "END WITH A STATEMENT, NOT A QUESTION, unless functionally necessary."
}
