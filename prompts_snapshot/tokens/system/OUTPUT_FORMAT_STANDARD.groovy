---
category: output_format
class: output_format
metadata:
  created_at: '2026-02-02'
  description: Standard conversational output format
  override_by:
  - AGENT
  use_case: Default for most agents - natural text
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
source_file: firestore_utils/uploads/OUTPUT_FORMAT_STANDARD.json
token_id: OUTPUT_FORMAT_STANDARD
uploaded_by: local_script
---
response_rules: [
    "Complete answer in Slack mrkdwn: *bold*, _italic_, lists with • or -",
    "No HTML, no standard Markdown (**bold**), no headers (#)",
    "Natural text flow, use bullet lists for good UX. Avoid multiple sentences in a row without line breaks.",
    "Use emojis to enhance tone and clarity."
]
