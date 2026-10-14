---
category: output_format
class: output_format
metadata:
  description: TutorExtractorAgent — structured JSON output contract
  override_by:
  - SYSTEM
source_file: firestore_utils/uploads/OUTPUT_FORMAT_TUTOR_EXTRACTOR.json
token_id: OUTPUT_FORMAT_TUTOR_EXTRACTOR
uploaded_by: local_script
---
format: "Respond with ONLY a single JSON object, no markdown fences, matching exactly:
{
  \"records\": [
    {\"text\": \"<one discrete memory, in English, self-contained>\", \"domain\": \"grammar_error|vocabulary_gap|topic_covered|progress_note\", \"tags\": [\"<short keyword>\"]}
  ],
  \"summary\": \"<rolling session summary, <=200 words>\"
}"

rules: [
    "records may be an empty array.",
    "summary is always present, even if unchanged in substance from before.",
    "Every record must have both text and domain; tags is optional and defaults to an empty array.",
]