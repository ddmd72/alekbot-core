import asyncio
import json
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.embedding_service import EmbeddingService
from src.adapters.gemini_adapter import GeminiAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig

async def debug_consolidation_prompt():
    print("🚀 Debugging Consolidation Prompt Assembly...")
    
    # 1. Setup minimal infrastructure
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    repo = FirestoreFactRepository(db_client, env_config)
    llm = GeminiAdapter(api_key=config["GEMINI_API_KEY"])
    embedding = EmbeddingService(api_key=config["GEMINI_API_KEY"])
    
    agent_config = AgentConfig(
        agent_id="debug_chronicler",
        agent_type="consolidation",
        llm_model="models/gemini-3-pro-preview"
    )
    
    agent = ConsolidationAgent(agent_config, llm, repo, embedding)
    
    # 2. Prepare test data from environment
    user_id = os.getenv("USER_ID")
    if not user_id:
        raise ValueError("USER_ID not found in .env - set your user_id for testing")
    
    test_messages = [
        {"role": "user", "text": "Я начал заниматься йогой каждое утро."},
        {"role": "alek_bot", "text": "Это отличная привычка! Как долго длятся ваши занятия?"},
        {"role": "user", "text": "Обычно около 30 минут. Это помогает мне сфокусироваться."}
    ]
    
    print(f"👤 User ID: {user_id}")
    print(f"📩 Messages: {len(test_messages)}")
    
    # 3. Simulate prompt assembly logic from _synthesize_session_facts
    conv_text = ""
    for msg in test_messages:
        role = msg.get("role") or "unknown"
        if role == "alek_bot":
            role = "assistant"
        text = msg.get("text") or ""
        conv_text += f"{role.upper()}: {text}\n"

    # Inject dynamic context: existing anchors
    existing_anchors_list = await repo.get_active_facts(user_id, tags=["anchor"])
    existing_anchors_str = ",\n".join([f'            "{a.text}"' for a in existing_anchors_list])

    # Reconstruct the prompt exactly as it is in the agent
    prompt = f"""
/**
 * Life Chronicler Agent
 * Purpose: Document objective facts about the User's life from conversations.
 * Specialty: Distinguishes primary sources (USER) from secondary sources (ASSISTANT).
 */
class LifeChronicler extends Agent {{
    
    // === USER KNOWLEDGE CONTEXT (About the person being chronicled) ===
    
    user_knowledge {{
        /**
         * Conversation to analyze.
         * Format: "ROLE: text\\nROLE: text\\n..."
         */
        conversation_transcript: \"\"\"
{conv_text.strip()}
\"\"\"
        
        /**
         * User's existing life principles (for semantic deduplication).
         * When extracting new PRINCIPLE → compare semantically against this list.
         * NOTE: Principles may be in mixed languages. Translate to English mentally for comparison.
         * If meaning is identical → skip. If new nuance → include.
         */
        established_life_principles: [
{existing_anchors_str}
        ]
    }}
    
    // === COGNITIVE DIRECTIVES (How the agent should think) ===
    
    cognitive_directives {{
        
        /**
         * Chain of Thought: Sequential reasoning framework.
         * Execute steps in order. Do not skip or reorder.
         */
        reasoning_framework {{
            
            @step_1 SOURCE_ATTRIBUTION {{
                question: "Who is the primary source of this information?"
                directive: "Extract ONLY from USER's direct statements."
                exception: "If ASSISTANT makes claim AND USER confirms → treat as USER's statement."
                rationale: "A chronicler documents what the person says, not what others infer."
            }}
            
            @step_2 EXTRACTION_CRITERIA_CHECKLIST {{
                question: "Does this information meet ALL extraction criteria?"
                checklist: [
                    "✓ BIOGRAPHICAL_SIGNIFICANCE: Is this significant for a life story? (Not small talk)",
                    "✓ OBJECTIVE_FACTUALITY: Is this measurable/verifiable? (Not just an inquiry)",
                    "✓ TEMPORAL_PERMANENCE: Will this be relevant in 1 year? (Not transient interest)"
                ]
                rule: "ALL three criteria must be TRUE to extract."
                distinguish: {{
                    ✅ EXTRACT: "User started daily yoga" → Significant + Verifiable + Permanent
                    ❌ SKIP: "User asks about yoga studios" → Significant but not Permanent (Inquiry)
                    ❌ SKIP: "User is tired today" → Verifiable but not Permanent (Transient state)
                }}
            }}
            
            @step_3 CATEGORIZATION {{
                question: "What type of fact is this?"
                directive: "Classify as STATE, EVENT, or PRINCIPLE."
                definitions: {{
                    STATE: "Current or long-term attribute, habits, recurring patterns. (e.g., 'User practices yoga daily')"
                    EVENT: "Past immutable occurrence, specific action start date. (e.g., 'User started yoga in Jan 2026')"
                    PRINCIPLE: "Guiding value or life philosophy. (must include tag 'anchor')"
                }}
                habit_rule: "Ongoing habits/routines should be classified as STATE (e.g. 'User runs every morning' -> STATE)."
            }}
            
            @step_4 SYNTHESIS {{
                question: "Can multiple claims be combined into a richer fact?"
                rule: "If multiple USER statements describe the SAME entity/activity → synthesize into one rich fact."
                example: {{
                    input: ["I do yoga every morning", "It lasts 30 mins", "It helps me focus"],
                    output: {{
                        "content": "User practices yoga every morning for 30 minutes to improve focus",
                        "type": "STATE",
                        "metadata": {{"frequency": "daily", "duration": "30m", "benefit": "focus"}}
                    }}
                }}
            }}

            @step_5 SEMANTIC_DEDUPLICATION {{
                question: "Does this PRINCIPLE duplicate an established life principle?"
                directive: "Compare new PRINCIPLE candidates against user_knowledge.established_life_principles."
                action: "If meaning overlaps (even if language differs) → skip. If distinct nuance → include."
                scope: "Apply ONLY to PRINCIPLE type. STATE and EVENT are time-bound (different values = different facts)."
            }}
            
            @step_6 FORMALIZATION {{
                question: "How should this be recorded?"
                directive: "Convert to English. Assign relevant tags. Extract structured metadata."
                requirements: {{
                    language: "English (translate if needed)"
                    format: "Concise, self-contained sentence"
                    tags: "Array of descriptive keywords"
                    metadata: "Object with structured data (e.g., {{weight_kg: 82, date: '2026-02'}})"
                }}
                rationale: "A chronicle uses consistent, searchable format."
            }}
        }}
    }}
    
    // === OUTPUT SPECIFICATION (Mandatory structure) ===
    
    output_specification {{
        
        @mandatory STRUCTURE {{
            rule: "Output MUST be valid JSON wrapped in markdown code block."
            format: '''```json
{{
  "new_facts": [],
  "new_anchors": []
}}
```'''
            constraint: "NEVER return: plain text, null, explanations, apologies, error messages."
        }}
        
        @mandatory SCHEMA {{
            definition: {{
                new_facts: "Array<Fact> - List of STATE or EVENT facts"
                new_anchors: "Array<Anchor> - List of PRINCIPLE facts (life philosophies)"
            }}
            
            Fact {{
                id: "String - Unique identifier (e.g., 'fact_weight_feb2026')"
                content: "String - The fact in English, concise and self-contained"
                tags: "Array<String> - Descriptive keywords (e.g., ['health', 'biometrics'])"
                type: "Enum - Must be 'STATE' or 'EVENT'"
                metadata: "Object - Structured data (e.g., {{weight_kg: 82}})"
            }}
            
            Anchor {{
                id: "String - Unique identifier (e.g., 'anchor_honesty')"
                content: "String - The principle in English, concise"
                tags: "Array<String> - Must include 'anchor' + descriptive keywords"
                type: "Enum - Must be 'PRINCIPLE'"
                metadata: "Object - Usually empty {{}}"
            }}
        }}
        
        @mandatory EXAMPLES {{
            
            example_state_fact: {{
                "id": "fact_weight_feb2026",
                "content": "User weighs 82kg as of February 2026",
                "tags": ["health", "biometrics", "weight"],
                "type": "STATE",
                "metadata": {{"weight_kg": 82, "date": "2026-02"}}
            }}
            
            example_event_fact: {{
                "id": "fact_paris_2020",
                "content": "User visited Paris in 2020",
                "tags": ["travel", "event", "europe"],
                "type": "EVENT",
                "metadata": {{"location": "Paris", "year": 2020}}
            }}
            
            example_anchor: {{
                "id": "anchor_direct_communication",
                "content": "Direct informal communication builds authentic connections",
                "tags": ["anchor", "principle", "communication", "values"],
                "type": "PRINCIPLE",
                "metadata": {{}}
            }}
            
            example_empty_result: {{
                "new_facts": [],
                "new_anchors": []
            }}
        }}
        
        @mandatory EDGE_CASES {{
            
            case_confirmation_pattern: {{
                input: \"\"\"
ASSISTANT: So you weigh 82kg?
USER: Да, точно
\"\"\",
                output: {{
                    "new_facts": [{{
                        "id": "fact_weight_confirmed",
                        "content": "User weighs 82kg",
                        "tags": ["health", "weight"],
                        "type": "STATE",
                        "metadata": {{"weight_kg": 82}}
                    }}],
                    "new_anchors": []
                }},
                reasoning: "USER confirmed ASSISTANT's claim → treat as primary source"
            }}
            
            case_assistant_inference: {{
                input: \"\"\"
USER: Привет
ASSISTANT: Based on history, you weigh 82kg
\"\"\",
                output: {{
                    "new_facts": [],
                    "new_anchors": []
                }},
                reasoning: "ASSISTANT inference without USER confirmation → skip (secondary source)"
            }}
            
            case_transient_inquiry: {{
                input: \"\"\"
USER: Какая погода в Париже?
ASSISTANT: Сейчас 15 градусов
\"\"\",
                output: {{
                    "new_facts": [],
                    "new_anchors": []
                }},
                reasoning: "USER inquiry about external fact → not about User themselves → skip"
            }}
            
            case_no_facts: {{
                input: \"\"\"
USER: Привет
ASSISTANT: Привет!
USER: Как дела?
ASSISTANT: Хорошо, спасибо!
\"\"\",
                output: {{
                    "new_facts": [],
                    "new_anchors": []
                }},
                reasoning: "Small talk without factual content → return empty arrays (VALID JSON)"
            }}
        }}
    }}
    
    // === EXECUTION METHOD ===
    
    method chronicle_life_facts() {{
        /**
         * Execute the cognitive_directives.reasoning_framework sequentially.
         * Return JSON following output_specification.SCHEMA.
         * If no facts found → return output_specification.EXAMPLES.example_empty_result.
         */
        return output_specification.STRUCTURE.format
    }}
}}
"""
    print("\n" + "="*50)
    print("ASSEMBLED PROMPT:")
    print("="*50)
    print(prompt)
    print("="*50)

if __name__ == "__main__":
    asyncio.run(debug_consolidation_prompt())
