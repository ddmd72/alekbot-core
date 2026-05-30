---
category: tools
class: tools
metadata:
  description: ConsolidationAgent v4 — tools section
  override_by:
  - AGENT
  source: split from COGNITIVE_PROCESS_CONSOLIDATION v3
source_file: firestore_utils/uploads/CONSOLIDATION_TOOLS.json
token_id: CONSOLIDATION_TOOLS
uploaded_by: local_script
---

@tool search_existing_facts(keywords: list, primary_query: str, alternative_query: str = "", limit: int = 20) {
    description: "Search Firestore for existing facts using semantic similarity"

    parameters: {
        keywords: "Domain keywords (nouns, names, objects, places, domain terms)"
        primary_query: "Primary semantic search phrase"
        alternative_query: "Alternative phrasing (optional)"
        limit: "Max results to return (default: 20)"
    }

    returns: "List[Dict] with structure: {fact_id: UUID (REQUIRED for update_fact/merge_facts), content: str, domain: str, temporal: str, state: str, tags: List[str], similarity: float}"

    usage_example: '''
    results = search_existing_facts(
        keywords=["weight", "kg", "biometrics"],
        primary_query="user weight 81 kg biometrics",
        limit=10
    )
    '''
}

@tool create_fact(content: str, fact_attributes: dict) {
    description: "Create NEW fact when candidate is orthogonal or new entity"

    parameters: {
        content: "Fact text (self-contained sentence)"
        fact_attributes: {
            domain: "FactDomain (BIOGRAPHICAL, POSSESSION, etc.)"
            temporal_class: "TemporalClass (PERMANENT, STABLE, DYNAMIC, EPHEMERAL)"
            state: "FactState (default: CURRENT)"
            context_priority: "ContextPriority (CRITICAL, HIGH, MEDIUM, LOW, HISTORICAL)"
            ttl_days: "Auto-calculated from temporal_class (can override)"
            tags: "List[str] - General classifications (domain keywords, dates)"
            metadata: "Dict - Structured data (numeric values, specific dates, details)"
            context: "Optional temporal context (e.g., 'Q1 2026 project')"
            reported_date: "When fact was recorded (now)"
        }
    }

    returns: "{fact_id, status, message}"

    usage_example: '''
    result = create_fact(
        content="User's weight was 83 kg in March 2025 (15 kg loss post-diet)",
        fact_attributes={
            "domain": "HEALTH",
            "temporal_class": "DYNAMIC",
            "state": "CURRENT",
            "context_priority": "MEDIUM",
            "ttl_days": 365,
            "tags": ["weight", "health", "biometrics", "2025", "diet"],
            "metadata": {
                "weight_kg": 83,
                "measurement_date": "2025-03",
                "diet_context": "post-fasting",
                "weight_change_kg": -15
            },
            "context": "Historical weight record",
            "reported_date": "2026-02-16T10:00:00"
        }
    )
    '''
}

@tool update_fact(fact_id: str, updates: dict) {
    description: "Update EXISTING fact (enrichment, new data point, or add missing metadata)"

    parameters: {
        fact_id: "UUID of fact to update"
        updates: {
            content: "Optional: new/enriched text"
            tags: "Optional: add tags"
            metadata: "Optional: add/update structured data"
            temporal_class: "Optional: can upgrade/downgrade"
            state: "Optional: change state"
            reported_date: "Always update to now"
        }
    }

    returns: "{fact_id, status, version, message}"

    usage_example: '''
    result = update_fact(
        fact_id="weight-fact-123",
        updates={
            "content": "User's weight: 80.5 kg (Feb 16), was 81 kg (Feb 7), 82.1 kg (Feb 5)",
            "tags": ["weight", "health", "biometrics", "tracking", "loss"],
            "metadata": {
                "weight_kg": 80.5,
                "measurement_date": "2026-02-16",
                "trend": "decreasing"
            },
            "reported_date": "2026-02-16T10:00:00"
        }
    )
    '''
}

@tool merge_facts(fact_ids: list, merged_content: str, fact_attributes: dict) {
    description: "Consolidate multiple facts into one enriched fact"

    parameters: {
        fact_ids: "List of UUIDs to merge"
        merged_content: "New combined text"
        fact_attributes: "Attributes for new fact (MUST include: domain, temporal_class, state, context_priority, tags, reported_date)"
    }

    returns: "{new_fact_id, old_fact_ids, old_facts_state: SUPERSEDED, status, message}"

    usage_example: '''
    result = merge_facts(
        fact_ids=["car-1", "car-2", "car-3"],
        merged_content="User owns 2012 Toyota Corolla (Plate: SAMPLE-0000) based in Springfield, with automatic gearbox, tinted windows",
        fact_attributes={
            "domain": "POSSESSION",
            "temporal_class": "STABLE",
            "state": "CURRENT",
            "context_priority": "MEDIUM",
            "tags": ["car", "toyota", "springfield"],
            "metadata": {
                "make": "Toyota",
                "model": "Corolla",
                "year": 2012,
                "plate": "SAMPLE-0000",
                "location": "Springfield"
            },
            "reported_date": "2026-02-16T10:00:00"
        }
    )
    '''
}
