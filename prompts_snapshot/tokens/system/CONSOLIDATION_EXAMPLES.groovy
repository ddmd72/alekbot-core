---
category: examples
class: examples
metadata:
  description: ConsolidationAgent v4 — examples section
  override_by:
  - AGENT
  source: split from COGNITIVE_PROCESS_CONSOLIDATION v3
source_file: firestore_utils/uploads/CONSOLIDATION_EXAMPLES.json
token_id: CONSOLIDATION_EXAMPLES
uploaded_by: local_script
---

example_1_update_weight: {
    conversation: "USER: Вчера я весил 70 кг"

    reasoning: '''
    <thinking>
    STEP 1 - EXTRACT:
    - Candidate: "User's weight is 70 kg (Oct 21, 2026)"

    STEP 2 - CLASSIFY:
    - Domain: HEALTH
    - Temporal: DYNAMIC (weight changes regularly)
    - State: CURRENT
    - Expiry: {kind: horizon, days: 90} (weight keeps changing; review if not said again)
    - Tags: ["weight", "health", "biometrics", "tracking"]
    - Metadata: {"weight_kg": 70, "measurement_date": "2026-10-21"}

    STEP 3 - SEARCH:
    Query: "user weight kg biometrics"
    Domain: HEALTH
    Results: [
        {
            fact_id: "weight-123",
            content: "User's current weight is 72 kg (Oct 14)",
            similarity: 0.95
        }
    ]

    STEP 4 - ANALYZE:
    - Top result: 95% similarity
    - Same metric (weight in kg)
    - Different data point (new measurement, 9 days later)
    - Match type: TIME SERIES UPDATE

    STEP 5 - DECIDE:
    Operation: UPDATE fact_id="weight-123"
    Reason: New data point in time series (weight tracking)
    Heuristic: "Candidate adds data points to time series"

    STEP 6 - EXECUTE:
    update_fact(
        fact_id="weight-123",
        updates={
            "content": "User's weight: 70 kg (Oct 21), was 72 kg (Oct 14), 74 kg (Oct 7)",
            "tags": ["weight", "health", "biometrics", "tracking", "loss"],
            "metadata": {
                "weight_kg": 70,
                "measurement_date": "2026-10-21",
                "trend": "decreasing"
            },
            "expiry": {"kind": "horizon", "days": 90},
            "reported_date": "2026-10-22T12:00+02:00"
        }
    )

    STEP 7 - VERIFY:
    ✓ Fact updated successfully (version 2)

    STEP 8 - REPORT:
    Action: UPDATE weight fact with new measurement
    </thinking>
    '''

    output: {
        "operations": [
            {
                "action": "UPDATE",
                "fact_id": "weight-123",
                "reason": "New data point in weight time series"
            }
        ]
    }
}

example_2_create_new_habit: {
    conversation: "USER: Я начал плавать по вечерам, 45 минут каждый день"

    reasoning: '''
    <thinking>
    STEP 1 - EXTRACT:
    - Candidate: "User started morning yoga practice (30 min daily)"

    STEP 2 - CLASSIFY:
    - Domain: HEALTH
    - Temporal: DYNAMIC (new habit, may change)
    - State: CURRENT
    - Expiry: {kind: horizon, days: 90} (a new habit may change; review if not said again)
    - Tags: ["yoga", "habit", "health", "morning", "routine"]
    - Metadata: {"duration_min": 45, "frequency": "daily", "time_of_day": "evening"}
    - Context: "Q1 2026 habit formation"

    STEP 3 - SEARCH:
    Query: "user yoga morning practice habit"
    Domain: HEALTH
    Results: [] (no matches)

    STEP 4 - ANALYZE:
    - No existing facts about yoga
    - Match type: NO MATCH

    STEP 5 - DECIDE:
    Operation: CREATE new fact
    Reason: New entity (yoga habit not previously recorded)
    Heuristic: "Candidate is new entity"

    STEP 6 - EXECUTE:
    create_fact(
        content="User started morning yoga practice (30 min daily) in Q1 2026",
        fact_attributes={
            "domain": "HEALTH",
            "temporal_class": "DYNAMIC",
            "state": "CURRENT",
            "context_priority": "MEDIUM",
            "expiry": {"kind": "horizon", "days": 90},
            "tags": ["yoga", "habit", "health", "morning", "routine"],
            "metadata": {
                "duration_min": 45,
                "frequency": "daily",
                "time_of_day": "evening",
                "start_quarter": "Q1_2026"
            },
            "context": "Q1 2026 habit formation",
            "reported_date": "2026-10-22T12:00+02:00"
        }
    )

    STEP 7 - VERIFY:
    ✓ Fact created successfully (fact_id: yoga-habit-456)

    STEP 8 - REPORT:
    Action: CREATE new yoga habit fact
    </thinking>
    '''

    output: {
        "operations": [
            {
                "action": "CREATE",
                "fact_id": "yoga-habit-456",
                "reason": "New habit not previously recorded"
            }
        ]
    }
}

example_3_discard_echo: {
    conversation: '''
    ASSISTANT: Based on our history, you weigh 75kg.
    USER: Да, всё верно.
    '''

    reasoning: '''
    <thinking>
    STEP 1 - EXTRACT:
    - Candidate: "User confirms weight is 75kg"

    STEP 2 - CLASSIFY:
    - Domain: HEALTH
    - Temporal: DYNAMIC
    - State: CURRENT

    STEP 3 - SEARCH:
    Query: "user weight 75kg"
    Domain: HEALTH
    Results: [
        {
            fact_id: "weight-123",
            content: "User's weight is 75kg",
            similarity: 0.98
        }
    ]

    STEP 4 - ANALYZE:
    - Top result: 98% similarity
    - ASSISTANT stated fact first (from RAG memory)
    - USER only confirmed, added NO new information
    - Match type: RAG ECHO TRAP

    STEP 5 - DECIDE:
    Operation: DISCARD
    Reason: User confirmation of ASSISTANT recall - no new information
    Heuristic: "Candidate is EXACT duplicate" + "Echo trap"

    STEP 6 - EXECUTE:
    Do NOT call any tool (DISCARD decision)

    STEP 7 - VERIFY:
    ✓ Candidate discarded

    STEP 8 - REPORT:
    Action: DISCARD (echo trap)
    </thinking>
    '''

    output: {
        "operations": [
            {
                "action": "DISCARD",
                "reason": "RAG echo trap - User confirmed existing fact without adding new info"
            }
        ]
    }
}

example_4_merge_car_details: {
    conversation: "USER: My car has tinted windows"

    reasoning: '''
    <thinking>
    STEP 1 - EXTRACT:
    - Candidate: "User's car has tinted windows"

    STEP 2 - CLASSIFY:
    - Domain: POSSESSION
    - Temporal: STABLE (car features don't change often)
    - State: CURRENT
    - Expiry: {kind: none} (stable ownership, changes only by an explicit update)
    - Tags: ["car", "features", "accessories"]

    STEP 3 - SEARCH:
    Query: "user car honda civic features"
    Domain: POSSESSION
    Results: [
        {
            fact_id: "car-1",
            content: "User owns 2015 Skoda Fabia",
            similarity: 0.85
        },
        {
            fact_id: "car-2",
            content: "Honda Civic has automatic gearbox",
            similarity: 0.82
        },
        {
            fact_id: "car-3",
            content: "Car is in Springfield",
            similarity: 0.78
        }
    ]

    STEP 4 - ANALYZE:
    - Multiple facts (3) about SAME entity (Honda Civic)
    - All high similarity (>0.75)
    - Facts are complementary (different aspects)
    - Combined fact would be more useful
    - Match type: MULTIPLE SIMILAR (same entity, different details)

    STEP 5 - DECIDE:
    Operation: MERGE car-1 + car-2 + car-3 + new info
    Reason: Consolidate scattered car details into one comprehensive fact
    Heuristic: "Multiple facts describe same entity" + "Facts are complementary"

    STEP 6 - EXECUTE:
    merge_facts(
        fact_ids=["car-1", "car-2", "car-3"],
        merged_content="User owns 2015 Skoda Fabia (Plate: SAMPLE-0000) based in Springfield, with automatic gearbox, tinted windows",
        fact_attributes={
            "domain": "POSSESSION",
            "temporal_class": "STABLE",
            "state": "CURRENT",
            "context_priority": "MEDIUM",
            "tags": ["car", "vehicle", "skoda", "springfield", "features"],
            "metadata": {
                "make": "Skoda",
                "model": "Fabia",
                "year": 2015,
                "plate": "SAMPLE-0000",
                "location": "Springfield",
                "features": ["tinted windows"]
            },
            "expiry": {"kind": "none"},
            "reported_date": "2026-10-22T12:00+02:00"
        }
    )

    STEP 7 - VERIFY:
    ✓ Facts merged successfully
    ✓ Old facts marked as SUPERSEDED (superseded_by = car-merged-789, set by the merge)
    ✓ New fact created (fact_id: car-merged-789)

    STEP 8 - REPORT:
    Action: MERGE 3 car facts + new details into comprehensive fact
    </thinking>
    '''

    output: {
        "operations": [
            {
                "action": "MERGE",
                "new_fact_id": "car-merged-789",
                "old_fact_ids": ["car-1", "car-2", "car-3"],
                "reason": "Consolidated scattered car details into one comprehensive fact"
            }
        ]
    }
}
