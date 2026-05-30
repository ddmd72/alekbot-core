---
category: output_format
class: output_format
metadata:
  author: system
  description: Email classifier output format — JSON array schema with field guidelines
  version: '1.0'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_EMAIL_CLASSIFIER.json
token_id: OUTPUT_FORMAT_EMAIL_CLASSIFIER
uploaded_by: local_script
---
output_schema {
    contract: "RAW JSON only — first char [, last char ]. No prose, no code block, no Markdown."

    json_schema: {
        "$schema": "http://json-schema.org/draft-07/schema",
        "type": "array",
        "items": {
            "type": "object",
            "required": ["email_id", "valuable", "valuable_type", "category", "fact", "tags"],
            "additionalProperties": false,
            "properties": {
                "email_id":      { "type": "string" },
                "valuable":      { "type": "boolean" },
                "valuable_type": { "type": "string", "enum": ["confirmed_event", "biographical_signal"] },
                "category":      { "oneOf": [{ "type": "string" }, { "type": "null" }] },
                "fact":          { "oneOf": [{ "type": "string" }, { "type": "null" }] },
                "tags":          { "type": "array", "items": { "type": "string" } }
            }
        }
    }
}

field_guidelines {
    email_id: [
        "Exact value from the input list — do not modify or abbreviate."
    ]

    valuable: [
        "true  — email is selected for storage.",
        "Always true in this output — non-valuable emails are omitted."
    ]

    valuable_type: [
        "confirmed_event     — email directly confirms a real-world event (booking, receipt, delivery, appointment).",
        "biographical_signal — email reveals biographical context about the user's life (family, school, relationships, memberships) even without a confirmed event."
    ]

    category: [
        "Required. One value from the taxonomy."
    ]

    fact: [
        "Required. One self-contained sentence in past tense with all key specifics.",
        "Include reference numbers, amounts, dates, and named entities.",
        "When attachment filenames were the key signal, name them explicitly."
    ]

    tags: [
        "Required: 3–8 lowercase items — category + specific entities."
    ]

    coverage: [
        "Array contains ONLY emails where valuable=true.",
        "Non-valuable emails are omitted entirely — do not include them."
    ]
}
