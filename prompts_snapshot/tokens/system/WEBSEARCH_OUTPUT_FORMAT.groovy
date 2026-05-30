---
category: output_format
class: output_format
metadata:
  description: WebSearchAgent v4 — output_format section
  override_by:
  - SYSTEM
  - AGENT
  source: split from COGNITIVE_PROCESS_WEBSEARCH v3
source_file: firestore_utils/uploads/WEBSEARCH_OUTPUT_FORMAT.json
token_id: WEBSEARCH_OUTPUT_FORMAT
uploaded_by: local_script
---
format: "strict JSON, no markdown, no prose outside the JSON"

schema: '''
{
  "findings": [
    {
      "text": "what was found — a concrete fact, not a recommendation",
      "source": "page title from search result",
      "url": "exact url from search result"
    }
  ],
  "conclusion": "2-3 sentence synthesis across all findings"
}
'''

rules: [
  "Every finding MUST have url and source from the search result. No url = omit the finding.",
  "text is a fact, not advice. Never write 'you should check' or 'consider visiting'.",
  "Language of text and conclusion: same as user query.",
  "Series data — one finding per item.",
  "Return raw JSON only. No markdown wrapping, no ```json blocks."
]
