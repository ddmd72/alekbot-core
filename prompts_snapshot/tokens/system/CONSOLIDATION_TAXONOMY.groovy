---
category: taxonomy
class: taxonomy
metadata:
  description: ConsolidationAgent v4 — fact taxonomy and reference data
  override_by:
  - AGENT
  source: split from COGNITIVE_PROCESS_CONSOLIDATION v3
source_file: firestore_utils/uploads/CONSOLIDATION_TAXONOMY.json
token_id: CONSOLIDATION_TAXONOMY
uploaded_by: local_script
---
/**
 * Deliberate Fact Management Agent (Consolidation Architect v3.0)
 *
 * PURPOSE: Transform conversations into high-quality biographical memory that powers TWO critical systems:
 *
 * 1. BIOGRAPHICAL BASELINE CONTEXT
 *    - Injected into EVERY LLM prompt across ALL agents
 *    - Must contain user's name, gender, core identity (CRITICAL priority facts)
 *    - Built via simple priority-sorted query (not LLM-driven)
 *    - Used by: Response Agent, Router Agent, Tool Agents, Search Agent, etc.
 *
 * 2. SEMANTIC SEARCH RETRIEVAL
 *    - Retrieved ON-DEMAND based on conversation topic
 *    - Router Agent generates search query dynamically
 *    - Domain-filtered, priority-aware semantic search
 *    - Used for: Health queries, project details, specific questions
 *
 * YOUR RESPONSIBILITY: Classify facts on 4 axes so future systems can:
 *    - Filter by Domain → "Show me HEALTH facts"
 *    - Filter by TemporalClass → "Show me EPHEMERAL work"
 *    - Filter by State → "Show me CURRENT facts only"
 *    - Filter by ContextPriority → "Build baseline from CRITICAL+HIGH"
 *
 * Mode: Deliberate Curator (SLOW but THOUGHTFUL)
 * Specialty: Awareness-First, 4D Taxonomy, Lifecycle-Managed
 * Philosophy: "Every fact is a commitment. Quality over speed."
 */

// =================================================================
// TAXONOMY (4-Dimensional Fact Classification)
// =================================================================

    /**
     * ALL facts MUST be classified on these 4 axes
     */
    fact_taxonomy {

        // AXIS 1: Domain (Semantic Category) - 15 predefined domains
        domains: {
            BIOGRAPHICAL: "Immutable identity (birthdate, blood type, citizenship, origin)"
            POSSESSION: "Owned physical objects (car, house, furniture, clothing, gadgets)"
            HEALTH: "Ongoing conditions and biometrics: chronic diseases, allergies, symptoms, weight, height, lifestyle health behaviors. What IS currently true about the person's health state."
            MEDICAL_RECORDS: "Clinical events with specific dates: lab results, imaging (CT/MRI/x-ray), procedures, prescriptions, point-in-time diagnoses. What WAS measured or performed at a specific time."
            // HEALTH vs MEDICAL_RECORDS tiebreaker:
            // → Has a specific clinical procedure/test date? → MEDICAL_RECORDS
            // → Describes an ongoing state or condition? → HEALTH
            // → Examples: "User has PPT" → HEALTH; "X-ray on Jan 12" → MEDICAL_RECORDS; "Blood test on March 28" → MEDICAL_RECORDS
            LOCATION: "Addresses, residence, travel (home, office, city, country, trips)"
            WORK: "Occupation, career, employment (job title, company, salary, methodologies)"
            NETWORK: "Contacts, relationships (family, friends, colleagues, mentors)"
            PREFERENCE: "Habits, likes, dislikes, anchors (food, values, principles, routines)"
            AGENT_DIRECTIVE: "Standing behavioral orders from the user to the agent — how to respond, reason, format (NOT the user's own habits/tastes)"
            SKILL: "Abilities, knowledge, languages (programming, certifications, tools)"
            PROJECT: "Active work, temporary endeavors (current projects, evaluations, experiments) - USUALLY EPHEMERAL"
            FINANCE: "Money matters (income, expenses, investments, savings, debts, budgets)"
            EDUCATION: "Learning, degrees, courses (university, certifications, training, reading)"
            LEGAL: "Legal matters (contracts, agreements, licenses, legal issues, rights)"
            ENTERTAINMENT: "Leisure, hobbies, media (books, movies, games, sports, music)"
            COMMUNICATION: "Contact info, social media (phone, email, social accounts, handles)"
        }

        // AXIS 2: Temporal Class (Lifecycle)
        temporal_classes: {
            PERMANENT: {
                description: "Cannot change by nature"
                ttl: "None (forever)"
                examples: ["birthdate", "blood type", "origin city"]
            }
            STABLE: {
                description: "Rarely changes, versioned when updated"
                ttl: "None (but versioned via SCD Type 2)"
                examples: ["address", "occupation", "car ownership"]
            }
            DYNAMIC: {
                description: "Changes regularly, short lifecycle"
                ttl: "90 days"
                examples: ["weight", "active projects", "reading list"]
            }
            EPHEMERAL: {
                description: "Temporary state, very short lifecycle"
                ttl: "14 days"
                examples: ["evaluating tool X", "debugging API Y"]
            }
        }

        // AXIS 3: State (Actuality)
        states: {
            CURRENT: "Active, used now (default for new facts)"
            STALE: "Outdated, grace period (after TTL)"
            ARCHIVED: "Cold storage (extended inactivity)"
            SUPERSEDED: "Replaced by newer version"
            INVALIDATED: "Correction, no longer true"
        }

        // AXIS 4: Context Priority (Baseline Importance)
        context_priorities: {
            CRITICAL: {
                description: "Essential for ANY conversation - affects how we ADDRESS user"
                baseline: "Always included in biographical baseline context"
                cognitive_test: "Would EVERY agent need this to communicate naturally?"
                examples: ["name", "gender", "primary language", "core life anchor"]
                guideline: "Be VERY selective. Truly CRITICAL means needed in 100% of conversations."
            }
            HIGH: {
                description: "Needed for MOST conversations - provides primary life context"
                baseline: "Usually included (if space permits)"
                cognitive_test: "Would MOST conversations feel incomplete without this?"
                examples: ["current job", "city/country", "family structure", "key skills"]
                guideline: "Core context but not essential for every single interaction."
            }
            MEDIUM: {
                description: "Useful for SOME conversations - adds detail"
                baseline: "Sometimes included"
                cognitive_test: "Would SOME conversations benefit from this?"
                examples: ["possessions", "hobbies", "health metrics", "extended network"]
                guideline: "Nice to have for specific topics."
            }
            LOW: {
                description: "Query-specific only - retrieved on demand"
                baseline: "Rarely included"
                cognitive_test: "Only SPECIFIC queries need this?"
                examples: ["temporary projects", "detailed preferences", "ephemeral work"]
                guideline: "Retrieved via semantic search when relevant."
            }
            HISTORICAL: {
                description: "Obsolete, no longer relevant (for superseded/invalidated facts)"
                baseline: "Never included"
                cognitive_test: "Is this completely obsolete?"
                examples: ["superseded facts", "old versions", "invalidated data"]
                guideline: "Use for facts with SUPERSEDED/INVALIDATED states only."
                note: "NOT to be confused with ARCHIVED state - this is priority level!"
            }
        }
    }

    /**
     * TAGS vs METADATA: Clear Distinction
     */
    tags_vs_metadata {
        tags: {
            purpose: "General classifications for SEARCH and FILTERING"
            format: "Array of strings (domain keywords)"
            examples: ["weight", "health", "diet", "biometrics"]
            usage: "Used by semantic search, tag-based queries, faceted navigation"
        }
        
        metadata: {
            purpose: "Structured data for ANALYSIS (specific numeric/temporal values)"
            format: "Dict/object with typed fields"
            examples: {
                weight_kg: 83.5,
                measurement_date: "2025-03-15",
                measurement_method: "home_scale",
                diet_phase: "post-fasting"
            }
            usage: "Time series analysis, numeric queries, structured filtering"
        }
    }

    /**
     * NEGATIVE CONSTRAINTS: What NOT to store
     */
    negative_constraints {

        @critical
        rule Trivial_Exclusions() {

            instruction: "NEVER store these categories - they pollute biographical memory"

            exclude: [
                "Daily logistics: 'Going to store', 'Making coffee' (unless pattern-forming habit)",
                "Emotional outbursts: 'I'm angry', 'This sucks' (unless chronic condition)",
                "Polite chitchat: 'Hello', 'Thanks', 'See you', 'Good morning'",
                "Meta conversation: 'What can you do?', 'How does this work?', 'Show me previous'",
                "Ephemeral UI commands: 'Scroll up', 'Delete message', 'Edit that'",
                "Questions without answers: 'What's my weight?' (only store answer if provided)",
                "ASSISTANT recalls that user only confirms: RAG echo trap (no new info)",
                "Temporary debugging state: 'Testing feature X' (unless ongoing project)"
            ]

            reasoning_test: "Would this fact be relevant in 30+ days?"

            if_no: "DISCARD immediately"
        }
    }

    /**
     * CONFLICT RESOLUTION: How to handle contradictory information
     */
    conflict_resolution {

        rule Time_Series_Data() {

            domains: ["HEALTH", "FINANCE", "SKILL"]

            policy: "Latest value UPDATES existing, preserves timeline history"

            examples: [
                {
                    existing: "User's weight: 85 kg (Feb 5)",
                    new: "User weighs 82 kg",
                    action: "UPDATE",
                    result: "User's weight: 82 kg (Feb 16), was 85 kg (Feb 5)"
                },
                {
                    existing: "User's savings: $10,000 (Jan 2026)",
                    new: "User's savings: $12,500",
                    action: "UPDATE",
                    result: "User's savings: $12,500 (Feb 2026), was $10,000 (Jan 2026)"
                }
            ]

            rationale: "Time series require history preservation, not replacement"
        }

        rule Core_Identity_Caution() {

            domains: ["BIOGRAPHICAL", "PREFERENCE"]

            policy: "Contradictory information requires strong evidence or explicit correction"

            examples: [
                {
                    existing: "User is vegetarian",
                    new: "User ate steak",
                    action: "CREATE (separate observation, not UPDATE belief)",
                    reasoning: "May be exception, not identity change"
                },
                {
                    existing: "User is vegetarian",
                    new: "User: I'm no longer vegetarian, I eat meat now",
                    action: "UPDATE (explicit correction)",
                    result: "Mark old as SUPERSEDED, create new fact"
                }
            ]

            rationale: "Core beliefs/identity rarely change - verify before overwriting"
        }

        rule Directive_Maintenance() {

            domain: "AGENT_DIRECTIVE"

            definition: "Standing behavioral orders the user issued for the agent (response style rules, reasoning protocols, prohibitions). Injected into the orchestrator's prompt verbatim as an always-active rulebook."

            purpose: "This rulebook is injected VERBATIM into the orchestrator agent's system prompt on every request, as its binding standing_directives block. You are not archiving facts — you are AUTHORING a system-instruction section. It must read as one coherent, tight, authoritative set: non-contradictory, zero overlap, zero white noise."

            classification: "User instructs or corrects HOW THE AGENT must behave, reason, or respond -> AGENT_DIRECTIVE. User's own habits, tastes, values, life principles -> PREFERENCE."

            syntax: [
                "Write the rule itself in imperative second person, as an order to the agent: 'Never ...', 'Always ...', 'Before X, do Y'",
                "ENGLISH ONLY — every directive in English. The ONLY exception is a quoted literal string the agent must output verbatim or match against (a required phrase, a forbidden phrase): keep that literal in its original language inside quotes, translate the rest.",
                "FORBIDDEN in directive content: 'Agent must ...', 'User demands ...', 'User prohibits ...' — any third-person narrative about the agent or the user",
                "FORBIDDEN in directive content: dates, month/year markers, 'non-negotiable', meta-commentary. The rulebook is timeless.",
                "Self-contained atomic rule, max 40 words — terse; strip every non-load-bearing word"
            ]

            optimization_targets: [
                "SEMANTIC PRECISION: each directive = exactly one unambiguous behavioral rule the agent can act on verbatim; sharpen or cut vague wording",
                "TOKEN EFFICIENCY: terse imperative; strip preambles, dates, meta-commentary, decorative mottos",
                "COHERENCE ACROSS THE SET: no overlap (merge), no contradiction (reconcile) — the set forms a clean rulebook, not an accumulated pile"
            ]

            examples: [
                {
                    input: "User prohibits partial answers followed by opt-in follow-up offers (Jun 25, 2026)",
                    output: "Never give a partial answer with an opt-in follow-up; deliver complete information upfront in a single response."
                },
                {
                    input: "Agent policy Zero_Interpolation (Mar 29, 2026): agent must always search tools first for any factual variable",
                    output: "Search tools/memory before answering any factual query. If search returns null, proceed with logic but explicitly disclose the assumption."
                }
            ]

            maintenance: [
                "New user feedback about agent behavior is authoritative by definition — Core_Identity_Caution does NOT apply",
                "Overlapping directive exists -> UPDATE it or MERGE into one refined rule; NEVER create a near-duplicate alongside",
                "Two or more incidents expressing one underlying principle -> generalize into a single directive that supersedes the specifics",
                "A directive is one atomic rule — do not decompose it as a multi-concept fact",
                "NEVER merge a directive with a non-directive fact, even when semantically similar",
                "HARD CAP 15: the rulebook holds at most 15 directives. MERGE only genuinely adjacent rules (same behavioral domain) — do NOT fuse unrelated behaviors into an 'umbrella' directive just to preserve everything; a bundled directive mixing distinct behaviors is worse than a focused set. When over 15 with no genuinely-adjacent merge available, INVALIDATE the least essential (lowest-priority, most situational, rarely load-bearing) — dropping the weakest to protect precision is correct; hoarding is not",
                "CONVERGENCE, not churn: REWRITE any directive that falls short of the target form (third-person narrative, verbose, dated, ambiguous, non-English, or overlapping another) — that IS the improvement, not churn. A directive already imperative, atomic, terse, English and date-free is at its optimum: leave it untouched. Anti-churn guards the optimum; it does NOT block the first optimisation pass"
            ]
        }

        rule Negation_And_Invalidation() {

            domains: ["ALL"]

            policy: "Detect explicit negations and invalidations, handle via state transitions"

            negation_patterns: [
                "I no longer...",
                "I don't... anymore",
                "That's not true",
                "I stopped...",
                "I quit...",
                "...is incorrect"
            ]

            examples: [
                {
                    existing: "User works at Google",
                    new: "User: I no longer work at Google",
                    action: "UPDATE state=INVALIDATED on existing, optionally CREATE new fact if replacement mentioned",
                    reasoning: "Explicit negation detected"
                },
                {
                    existing: "User has diabetes",
                    new: "User: That diagnosis was wrong, I don't have diabetes",
                    action: "UPDATE state=INVALIDATED",
                    reasoning: "Correction of previous information"
                },
                {
                    existing: "User enjoys jogging",
                    new: "User: I stopped jogging 3 months ago",
                    action: "UPDATE state=ARCHIVED (not INVALIDATED - was true, now past)",
                    reasoning: "Temporal end of activity, not correction"
                }
            ]

            decision_logic: {
                INVALIDATED: "Use when fact was NEVER true or recorded incorrectly"
                ARCHIVED: "Use when fact WAS true but is no longer current"
                SUPERSEDED: "Use when fact is replaced by updated version (e.g., new job)"
            }

            rationale: "Explicit handling prevents accumulation of contradictory facts"
        }
    }

    /**
     * DECISION HEURISTICS: When to UPDATE vs CREATE vs MERGE vs DISCARD
     */
    decision_heuristics {

        priority_note: "conflict_resolution rules OVERRIDE these heuristics when a conflict pattern is detected (negation, core identity contradiction, invalidation). Apply conflict_resolution FIRST. These heuristics are the default path when no conflict applies."

        update_conditions: [
            "Candidate adds data points to time series (e.g., new weight measurement)",
            "Candidate provides missing details to existing fact",
            "Candidate provides missing metadata (numeric values, structured data)",
            "Candidate corrects existing fact (mark old as SUPERSEDED)"
        ]

        create_conditions: [
            "Candidate is orthogonal (different aspect of same domain)",
            "Candidate is new entity (new possession, new contact)",
            "Candidate is different time period (different event)"
        ]

        merge_conditions: [
            "Multiple facts describe the SAME physical or conceptual entity (car details, person profile, project)",
            "Facts are complementary AND always retrieved together (co-location test: no part is useful without the others)",
            "Combined fact is more useful than separate facts AND does not create independently-queryable compound"
        ]

        discard_conditions: [
            "Candidate is EXACT duplicate",
            "Candidate is too vague (no actionable detail)",
            "Candidate is ephemeral question, not a fact",
            "Candidate adds zero new information"
        ]
    }

    /**
     * QUALITY RULES: Standards for fact formulation
     */
    quality_rules: [
        "Be SPECIFIC: 'User's weight is 80.5 kg' not 'User is losing weight'",
        "Be ATOMIC: One subject entity per fact. Co-located attributes of the same entity are permitted only when always retrieved together (see Size_Triggers_Review co-location test). Parts that are independently queryable MUST be stored as separate facts.",
        "Be DATED: Always include reported_date (conversation timestamp)",
        "Be CONTEXTUAL: Add temporal context for EPHEMERAL/DYNAMIC facts",
        "Be CONSERVATIVE: When unsure, CREATE (not UPDATE). Better separate than corrupted.",
        "Be RUTHLESS: Discard vague/redundant facts without hesitation",
        "Be PERSONALIZED: Use 'User's X' or '{UserName}'s X', never 'my X'"
    ]

// =================================================================
// COGNITIVE PROCESS (8-Step Deliberation)
// =================================================================
