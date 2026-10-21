---
category: output_format
class: output_format
metadata:
  description: DriveTranscriberAgent — output_format section (mirrors _RESPONSE_SCHEMA)
  override_by:
  - SYSTEM
  - AGENT
  source: 'draft 2026-10-21: drive transcripts (USER_DRIVE_INDEXING_RFC §27.5), not
    uploaded'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DRIVE_TRANSCRIBER.json
token_id: OUTPUT_FORMAT_DRIVE_TRANSCRIBER
uploaded_by: local_script
---
format: "One JSON object and nothing else: no markdown fences, no text before or after it."

shape: '{"language_codes": [string], "speakers": integer, "no_speech": boolean, "segments": [{"start": string, "speaker": integer or null, "text": string}]}'

fields: [
    "Every field is required.",
    "start: 'MM:SS' below one hour, 'H:MM:SS' from one hour; counted from the start of this window; never an integer number of seconds.",
    "speaker: an integer starting at 1, stable inside the window, or null when there is a single speaker.",
    "text: the words of the segment exactly as spoken.",
    "segments: in order of time; an empty array only when no_speech is true.",
]
