---
category: output_format
class: output_format
metadata:
  description: EmailSearchAgent — output_format describing JSON search-key structure
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/EMAILSEARCH_OUTPUT_FORMAT.json
token_id: EMAILSEARCH_OUTPUT_FORMAT
uploaded_by: local_script
---
output_schema {
    contract: "RAW JSON only — first char {, last char }. No prose, no code block."

    json_schema: {
        "$schema": "http://json-schema.org/draft-07/schema",
        "type": "object",
        "required": ["primary_query", "alternative_query", "tags", "date_from", "date_to"],
        "additionalProperties": false,
        "properties": {
            "primary_query":     { "type": "string", "maxLength": 60 },
            "alternative_query": { "type": "string", "maxLength": 60 },
            "tags": {
                "type": "array",
                "items": { "type": "string" },
                "minItems": 3,
                "maxItems": 5
            },
            "date_from": { "type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
            "date_to":   { "type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$" }
        }
    }
}

field_guidelines {
    primary_query: [
        "Phrase describing what the indexer's extracted fact would say — the captured event or information.",
        "Not the email framing. No 'email about', 'message from', 'confirmation of'.",
        "In ENGLISH. Max 60 characters."
    ]

    alternative_query: [
        "Orthogonal aspect of the same subject — counterparty, amount, reference, outcome, or category.",
        "Zero verbatim overlap with primary_query.",
        "In ENGLISH. Max 60 characters."
    ]

    tags: [
        "3–5 short English terms (1–2 words each) that an indexer would use as category tags.",
        "Must not repeat words from primary_query or alternative_query."
    ]

    date_from: [
        "Start of the date range inferred from the query. YYYY-MM-DD format.",
        "Null if the query contains no time signal."
    ]

    date_to: [
        "End of the date range inferred from the query (inclusive). YYYY-MM-DD format.",
        "Null if the query contains no time signal."
    ]
}
