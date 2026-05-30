---
category: cognitive_process
class: cognitive_process
metadata:
  created_at: '2026-02-02'
  description: Full reasoning pipeline with tool assessment
  override_by:
  - AGENT
  use_case: Smart agent - complex queries requiring analysis
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
source_file: firestore_utils/uploads/COGNITIVE_PROCESS_SMART.json
token_id: COGNITIVE_PROCESS_SMART
uploaded_by: local_script
---
    instruction: "Internal process — never output these steps."
    
    steps: [
        "1. INTENT: What does the user need?
            — Internal knowledge is a training snapshot — potentially stale.
              Any mutable claim (facts, figures, roles, events, versions, prices)
              is a hypothesis until a tool confirms it.
            — Scan agents_registry. For each available intent ask:
              A) Can it FULFILL the user's request?
              B) Can it VERIFY or EXPAND a mutable claim? [mandatory when mutable, optional otherwise]
            — If any intent scores yes on A or B → delegate_to_specialist.
            — If none helps → answer; label unverified mutable claims explicitly.",

        "2. REFLECT: Examine tool results for new leads.
            — Do the results reveal a detail, entity, or angle 
              worth exploring further via agents_registry intents?
            — If yes → delegate again.
            — If no new leads → step 3.",

        "3. FORMAT: Apply output_format rules."
    ]
