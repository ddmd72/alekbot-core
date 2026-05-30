---
category: output_format
class: output_specification
metadata:
  description: ConsolidationAgent v4 — output_specification section
  override_by:
  - AGENT
  source: split from COGNITIVE_PROCESS_CONSOLIDATION v3
source_file: firestore_utils/uploads/CONSOLIDATION_OUTPUT_SPEC.json
token_id: CONSOLIDATION_OUTPUT_SPEC
uploaded_by: local_script
---

format: "JSON object with operations list"

schema: {
    operations: "Array of operation objects"
}

operation_object: {
    action: "Enum: CREATE | UPDATE | MERGE | DISCARD"
    fact_id: "String (for UPDATE/MERGE) - UUID of affected fact"
    new_fact_id: "String (for MERGE) - UUID of newly created merged fact"
    old_fact_ids: "Array (for MERGE) - UUIDs of superseded facts"
    reason: "String - Explanation for decision"
}

decomposition_reporting: {
    description: "When Size_Triggers_Review triggers decomposition of an existing compound fact, report using existing action types — no special type needed."
    convention: [
        "CREATE per atomic part: reason must include 'Decomposed from <old-fact-id>'",
        "UPDATE old fact with state=SUPERSEDED: reason must list new fact IDs it was split into",
        "DISCARD for extracted parts that fail Trivial_Exclusions 30-day test"
    ]
    example: [
        {"action": "CREATE", "fact_id": "ppt-diagnosis-new", "reason": "Decomposed from ppt-compound-old: diagnosis extracted as independent fact"},
        {"action": "CREATE", "fact_id": "ppt-protocol-new", "reason": "Decomposed from ppt-compound-old: rehabilitation protocol extracted as independent fact"},
        {"action": "UPDATE", "fact_id": "ppt-compound-old", "reason": "SUPERSEDED — decomposed into ppt-diagnosis-new, ppt-protocol-new"},
        {"action": "DISCARD", "reason": "Decomposed from ppt-compound-old: yoga disclaimer fails 30-day relevance test"}
    ]
}

example_output: '''
{
    "operations": [
        {
            "action": "UPDATE",
            "fact_id": "weight-123",
            "reason": "Added new weight measurement to time series"
        },
        {
            "action": "CREATE",
            "fact_id": "yoga-456",
            "reason": "New habit not previously recorded"
        },
        {
            "action": "DISCARD",
            "reason": "Too vague - no actionable detail"
        },
        {
            "action": "MERGE",
            "new_fact_id": "car-789",
            "old_fact_ids": ["car-1", "car-2", "car-3"],
            "reason": "Consolidated scattered car details"
        }
    ]
}
'''
