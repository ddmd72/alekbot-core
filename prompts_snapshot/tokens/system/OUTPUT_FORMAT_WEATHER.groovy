---
category: output_format
class: output_format
metadata:
  created_at: '2026-02-02'
  description: Structured weather forecast formatting
  override_by:
  - AGENT
  use_case: WebSearch agent when returning weather data
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
source_file: firestore_utils/uploads/OUTPUT_FORMAT_WEATHER.json
token_id: OUTPUT_FORMAT_WEATHER
uploaded_by: local_script
---
style: "Slack mrkdwn (no headers, use *bold*)"
structure: "For weather: output lines as Day: min/max °C | condition | humidity | wind. Use explicit min/max labels if possible."
constraints: [
    "Use English day names (Monday, Tuesday, ...).",
    "If only one temperature is available, output it as \"Temp: X°C\" (do NOT duplicate).",
    "If min/max not available, use \"—\" for the missing value.",
    "If humidity or wind missing, use \"—\".",
    "Do not output tables or bullet lists."
]
