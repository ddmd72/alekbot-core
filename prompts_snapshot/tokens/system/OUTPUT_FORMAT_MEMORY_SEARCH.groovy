---
category: output_format
class: output_format
metadata:
  author: system
  description: Memory search output format — describes JSON structure in prompt text
    (no response_schema)
  version: '1.0'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_MEMORY_SEARCH.json
token_id: OUTPUT_FORMAT_MEMORY_SEARCH
uploaded_by: local_script
---
instruction: "Output a single valid JSON object. No text before or after."

structure: {
    "keywords":          ["<term1>", "<term2>", "<term3>"],
    "primary_query":     "<main search phrase>",
    "alternative_query": "<alternative phrasing>",
    "domains":           ["<domain1>"]
}

field_rules: {

    keywords: [
        "Array of 3–5 short English terms (1–2 words each).",
        "Must NOT overlap with primary_query or alternative_query words.",
        "Hard limit: minimum 3, maximum 5 items."
    ]

    primary_query: [
        "One phrase describing what the KB fact itself would say.",
        "No framing words like 'user' or 'my'.",
        "Maximum 50 characters."
    ]

    alternative_query: [
        "Rephrasing of the same subject using synonyms or a completely different angle.",
        "Zero verbatim overlap with primary_query.",
        "Maximum 50 characters."
    ]

    domains: [
        "1–2 values from the enum below. Always include at least one.",
        "Enum: biographical, possession, health, medical_records, location, work,",
        "      network, preference, skill, project, finance, education, legal,",
        "      entertainment, communication."
    ]
}

critical: "Output ONLY the JSON object. All four fields are required. JSON must start with { and end with }."
