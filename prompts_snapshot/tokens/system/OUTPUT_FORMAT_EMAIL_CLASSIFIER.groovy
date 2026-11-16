---
category: output_format
class: output_format
metadata:
  author: system
  description: 'Email gate output format (email index v2 §4, slice 9b): one keep/drop
    decision with its reason for every listed mail'
  version: '2.0'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_EMAIL_CLASSIFIER.json
token_id: OUTPUT_FORMAT_EMAIL_CLASSIFIER
uploaded_by: local_script
---
output_schema {
    contract: "RAW JSON only: the first character is [, the last is ]. No prose, no code block, no Markdown."

    json_schema: {
        "$schema": "http://json-schema.org/draft-07/schema",
        "type": "array",
        "items": {
            "type": "object",
            "required": ["email_id", "decision", "reason"],
            "additionalProperties": false,
            "properties": {
                "email_id": { "type": "string" },
                "decision": { "type": "string", "enum": ["keep", "drop"] },
                "reason":   { "type": "string" }
            }
        }
    }
}

field_guidelines {
    email_id: [
        "The mail's email_id exactly as the input array gives it."
    ]

    decision: [
        "keep or drop."
    ]

    reason: [
        "One value from KEEP REASONS when the decision is keep, from DROP REASONS when it is drop, spelled exactly as listed."
    ]

    coverage: [
        "One object for every mail of the input array, kept and dropped alike, each mail once."
    ]
}
