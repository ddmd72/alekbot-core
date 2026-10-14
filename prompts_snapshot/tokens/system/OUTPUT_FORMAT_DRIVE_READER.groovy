---
category: output_format
class: output_format
metadata:
  description: DriveDocumentReaderAgent — output_format section (mirrors _RESPONSE_SCHEMA)
  override_by:
  - SYSTEM
  - AGENT
  source: 'v1 2026-10-13: User Drive indexing Slice 2+4'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DRIVE_READER.json
token_id: OUTPUT_FORMAT_DRIVE_READER
uploaded_by: local_script
---
format: "One JSON object and nothing else: no markdown fences, no text before or after it."

shape: '{"text": string, "brief": null | {"doc_type": string, "description": string, "doc_date": string | null, "parties": [string], "amount": null | {"value": string, "currency": string}}}'

fields: [
    "text: the verbatim transcription, tables as Markdown tables; an empty string when the input holds no text.",
    "brief: an object only when the task says to fill it; null otherwise.",
    "brief.doc_type: one of invoice, receipt, contract, statement, letter, ticket, certificate, form, other.",
    "brief.description: one line, in the document's language, saying what this document is.",
    "brief.doc_date: YYYY-MM-DD, or null.",
    "brief.parties: at most five names, as written.",
    "brief.amount: value as printed; currency as an ISO 4217 code when the document makes it clear, else the symbol as printed. null when there is no amount.",
]
