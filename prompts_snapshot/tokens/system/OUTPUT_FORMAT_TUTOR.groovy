---
category: output_format
class: output_format
metadata:
  description: TutorAgent — structured JSON output contract (full_response + response_summary)
  override_by:
  - SYSTEM
source_file: firestore_utils/uploads/OUTPUT_FORMAT_TUTOR.json
token_id: OUTPUT_FORMAT_TUTOR
uploaded_by: local_script
---
output_schema {
    contract: "RAW JSON only — first char '{', last char '}'. No prose, no code blocks."

    schema: {
        "type": "object",
        "required": ["full_response", "response_summary"],
        "properties": {
            "full_response":    { "type": "string" },
            "response_summary": { "type": "string", "maxLength": 300 }
        }
    }
}

field_guidelines {
    full_response: "The reply shown to the user. Natural conversational tone, no markdown headers."
    response_summary: "Max 300 chars, plain text. A condensed version of full_response's own content — what you actually said, not a label for what topic or correction the turn covered. Purpose: pure token-saving — in later turns, older replies are shown to you as this summary instead of the full text, so it must preserve enough of what you said that the conversation stays coherent without the original."
}

examples {
    example: '''
    {
        "full_response": "¡Casi! Con 'ojalá' necesitas el subjuntivo: 'ojalá que vengas', no 'ojalá que vienes'.",
        "response_summary": "'Ojalá' takes the subjunctive, not indicative — correct form is 'ojalá que vengas', not 'ojalá que vienes'."
    }
    '''
}
