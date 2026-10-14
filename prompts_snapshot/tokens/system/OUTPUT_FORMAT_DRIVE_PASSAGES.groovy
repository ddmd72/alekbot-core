---
category: output_format
class: output_format
metadata:
  description: DrivePassageAgent — output_format section (mirrors _RESPONSE_SCHEMA)
  override_by:
  - SYSTEM
  - AGENT
  source: 'v2 draft 2026-10-13: Search v2 (USER_DRIVE_INDEXING_RFC §25.4), not uploaded'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DRIVE_PASSAGES.json
token_id: OUTPUT_FORMAT_DRIVE_PASSAGES
uploaded_by: local_script
---
format: "One JSON object and nothing else: no markdown fences, no text before or after it."

shape: '{"passages": [{"index": integer, "items": [{"type": "keyword" | "question", "text": string, "evidence": string}]}]}'

fields: [
    "passages: one entry per passage you were given.",
    "index: the passage's number as given.",
    "items: keywords and questions, each with its evidence; an empty list when there is nothing to search for.",
    "type: keyword or question.",
    "evidence: three to twelve whole words copied exactly from that passage.",
]
