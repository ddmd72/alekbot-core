---
category: output_format
class: output_format
metadata:
  description: DriveDocumentReaderAgent — output_format section (mirrors _RESPONSE_SCHEMA)
  override_by:
  - SYSTEM
  - AGENT
  source: 'v2 draft 2026-10-14: capture, no brief doc_type (USER_DRIVE_INDEXING_RFC §26.3, Δ64), not uploaded'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DRIVE_READER.json
token_id: OUTPUT_FORMAT_DRIVE_READER
uploaded_by: local_script
---
format: "One JSON object and nothing else: no markdown fences, no text before or after it."

shape: '{"text": string, "capture": "scan" | "document_photo" | "screenshot" | "digital", "brief": null | {"description": string, "doc_date": string | null, "parties": [string], "amount": null | {"value": string, "currency": string}}}'

fields: [
    "text: the verbatim transcription, tables as Markdown tables; an empty string when the input holds no text.",
    "capture: how the input was made. scan: an image a scanner made of paper. document_photo: a camera photograph of paper. screenshot: a capture of a screen. digital: a page made by software, never on paper.",
    "brief: an object only when the task says to fill it; null otherwise.",
    "brief.description: one line, in the document's language, saying what this document is.",
    "brief.doc_date: YYYY-MM-DD, or null.",
    "brief.parties: at most five names, as written.",
    "brief.amount: value as printed; currency as an ISO 4217 code when the document makes it clear, else the symbol as printed. null when there is no amount.",
]
