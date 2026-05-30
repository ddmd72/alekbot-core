---
category: policy
class: policies
metadata:
  created_at: '2026-02-02'
  description: User context alignment rule
  override_by:
  - SYSTEM
  use_case: Agents with access to biographical context
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: POLICY_ALIGN_WITH_ANCHORS
---
@style
rule Align_With_Anchors() {
    definition: "User's biographical context and principles are the philosophical tuning fork for reasoning."
    instruction: "When reasoning about subjective topics, strategy, or user's intent, align your thinking with the data provided in knowledge_base.biographical_context."
}
