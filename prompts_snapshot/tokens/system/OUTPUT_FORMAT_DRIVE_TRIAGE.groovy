---
category: output_format
class: output_format
metadata:
  description: DriveImageTriageAgent v2 — output_format section (mirrors _RESPONSE_SCHEMA)
  override_by:
  - SYSTEM
  - AGENT
  source: 'v2 draft 2026-10-13: Search v2 (USER_DRIVE_INDEXING_RFC §25.4), not uploaded'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DRIVE_TRIAGE.json
token_id: OUTPUT_FORMAT_DRIVE_TRIAGE
uploaded_by: local_script
---
format: "One JSON object and nothing else: no markdown fences, no text before or after it."

shape: '{"images": [{"index": integer, "verdict": "text" | "photo", "description": string, "objects": [{"name": string, "forms": [string]}], "scene": string, "visible_text": string}]}'

fields: [
    "images: one entry per image you were given.",
    "index: the image's number as given in its label.",
    "verdict: text or photo.",
    "description: for photo, one or two English sentences; for text, an empty string.",
    "objects: for photo, up to eight, each with an English name and up to six forms; for text, an empty list.",
    "scene: for photo, up to six English words; for text, an empty string.",
    "visible_text: for photo, the legible words as written, else an empty string; for text, an empty string.",
]
