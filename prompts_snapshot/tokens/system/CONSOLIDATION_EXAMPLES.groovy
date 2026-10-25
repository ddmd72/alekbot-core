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
    - Different data point (new measurement, 7 days later)
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
    - Candidate: "User started evening swimming practice (45 min daily)"

    STEP 2 - CLASSIFY:
    - Domain: HEALTH
    - Temporal: DYNAMIC (new habit, may change)
    - State: CURRENT
    - Expiry: {kind: horizon, days: 90} (a new habit may change; review if not said again)
    - Tags: ["swimming", "habit", "health", "evening", "routine"]
    - Metadata: {"duration_min": 45, "frequency": "daily", "time_of_day": "evening"}
    - Context: "Q1 2026 habit formation"

    STEP 3 - SEARCH:
    Query: "user swimming evening practice habit"
    Domain: HEALTH
    Results: [] (no matches)

    STEP 4 - ANALYZE:
    - No existing facts about swimming
    - Match type: NO MATCH

    STEP 5 - DECIDE:
    Operation: CREATE new fact
    Reason: New entity (swimming habit not previously recorded)
    Heuristic: "Candidate is new entity"

    STEP 6 - EXECUTE:
    create_fact(
        content="User started evening swimming practice (45 min daily) in Q1 2026",
        fact_attributes={
            "domain": "HEALTH",
            "temporal_class": "DYNAMIC",
            "state": "CURRENT",
            "context_priority": "MEDIUM",
            "expiry": {"kind": "horizon", "days": 90},
            "tags": ["swimming", "habit", "health", "evening", "routine"],
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
    ✓ Fact created successfully (fact_id: swim-habit-456)

    STEP 8 - REPORT:
    Action: CREATE new swimming habit fact
    </thinking>
    '''

    output: {
        "operations": [
            {
                "action": "CREATE",
                "fact_id": "swim-habit-456",
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

example_3b_memory_review_reply: {
    rule: "Domain_Scope.memory_review_reply: the quoted fact belongs to the memory review; only what the answer adds is recorded"

    case_1_correction: {
        conversation: '''
        USER: [System: memory review q=rv-71c2 — Alek asked the owner about a fact he could not confirm]
        ASSISTANT: Is this still true? "User trains at the FitLife gym on Main Street." 1 Yes · 2 No longer · 3 Never · 4 Don't remember
        USER: [Reply to memory review q=rv-71c2] 3 — it's Iron Temple now
        ASSISTANT: Noted — I'll take it into account.
        '''

        reasoning: '''
        <thinking>
        STEP 1 - EXTRACT:
        - The first USER part is the frame of the question: written by the system, not by the owner: nothing to extract
        - The ASSISTANT question quotes a stored fact: an echo, DISCARD
        - "[Reply to memory review q=rv-71c2] 3 — it's Iron Temple now": the owner answering the memory review
          - "3" adds nothing: the memory review applies it to the quoted fact
          - "it's Iron Temple now" is a correction naming the true value
        - Candidate: "User trains at the Iron Temple gym"

        STEP 2 - CLASSIFY:
        - Domain: HEALTH
        - Temporal: DYNAMIC
        - Expiry: {kind: horizon, days: 180} (a current activity)

        STEP 3 - SEARCH:
        Query: "user gym training"
        Results: [
            {
                fact_id: "gym-88",
                content: "User trains at the FitLife gym on Main Street.",
                similarity: 0.83
            }
        ]

        STEP 4 - ANALYZE:
        - gym-88 is the fact the question quoted: it belongs to the memory review, which applies the answer
        - No fact says Iron Temple yet

        STEP 5 - DECIDE:
        Operation: CREATE the Iron Temple fact only
        Leave gym-88 untouched: no update_fact call for it, no invalidation, no extension

        STEP 6 - EXECUTE:
        create_fact(
            content="User trains at the Iron Temple gym",
            fact_attributes={
                "domain": "HEALTH",
                "temporal_class": "DYNAMIC",
                "state": "CURRENT",
                "context_priority": "LOW",
                "expiry": {"kind": "horizon", "days": 180},
                "tags": ["gym", "training", "fitness"],
                "metadata": {"gym": "Iron Temple"},
                "reported_date": "2026-10-22T12:00+02:00"
            }
        )

        STEP 8 - REPORT:
        Action: CREATE the corrected value; the quoted fact is left to the memory review
        </thinking>
        '''

        output: {
            "operations": [
                {
                    "action": "CREATE",
                    "fact_id": "gym-iron-temple-1",
                    "reason": "Correction in a memory-review reply: the true value is a new fact; the quoted fact belongs to the review"
                }
            ]
        }
    }

    case_2_digit_only: {
        conversation: '''
        USER: [System: memory review q=rv-90aa — Alek asked the owner about a fact he could not confirm]
        ASSISTANT: Is this still true? "User is learning Portuguese." 1 Yes · 2 No longer · 3 Never · 4 Don't remember
        USER: [Reply to memory review q=rv-90aa] 2
        ASSISTANT: Thank you, recorded.
        '''

        reasoning: "The reply is the digit 2 and nothing else: it adds nothing, and the memory review applies it to the quoted fact. The question pair is an echo. No tool call."

        output: {
            "operations": [
                {
                    "action": "DISCARD",
                    "reason": "Memory-review reply with no new information (Domain_Scope.memory_review_reply)"
                }
            ]
        }
    }

    case_3_plain_message: {
        conversation: '''
        USER: [System: memory review q=rv-90aa — Alek asked the owner about a fact he could not confirm]
        ASSISTANT: Is this still true? "User is learning Portuguese." 1 Yes · 2 No longer · 3 Never · 4 Don't remember
        USER: What's the weather tomorrow?
        ASSISTANT: Sunny, around 24°C.
        USER: 2
        '''

        reasoning: "The last message has no [Reply to memory review q=<id>] marker, so it is not an answer: the ordinary rules apply. A bare digit states no fact (Trivial_Exclusions), and the question pair stays an echo. No tool call."

        output: {
            "operations": [
                {
                    "action": "DISCARD",
                    "reason": "A plain message, not a memory-review reply: a bare digit states no fact"
                }
            ]
        }
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
    Query: "user car skoda fabia features"
    Domain: POSSESSION
    Results: [
        {
            fact_id: "car-1",
            content: "User owns 2015 Skoda Fabia",
            similarity: 0.85
        },
        {
            fact_id: "car-2",
            content: "Skoda Fabia has automatic gearbox",
            similarity: 0.82
        },
        {
            fact_id: "car-3",
            content: "Car is in Springfield",
            similarity: 0.78
        }
    ]

    STEP 4 - ANALYZE:
    - Multiple facts (3) about SAME entity (Skoda Fabia)
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
