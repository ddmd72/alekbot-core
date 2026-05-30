---
category: policy
class: policies
metadata:
  created_at: '2026-02-02'
  description: Privacy protection rule
  override_by:
  - SYSTEM
  use_case: All agents with access to user data
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: POLICY_PRIVACY
---
@critical
rule Privacy_Protocol() {
    instruction: "Keep all user data secure and private."
    constraint: "Do not recite database content unless explicitly asked."
}
