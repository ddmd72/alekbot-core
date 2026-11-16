---
category: output_format
class: output_format
metadata:
  description: LelikSummarizerAgent — {title, summary} JSON output contract
  override_by:
  - SYSTEM
source_file: firestore_utils/uploads/OUTPUT_FORMAT_LELIK_SUMMARIZER.json
token_id: OUTPUT_FORMAT_LELIK_SUMMARIZER
uploaded_by: call_file_summarizer
---
format: "Code reads your output, so it is one JSON object and nothing else: no markdown fences, no text before or after it. It has two string fields. summary is the note itself, exactly as the rules above describe it; code passes it on unchanged to where the note goes. title is a short name for the call, saying in a few words what it was about; code may show it next to the call's date and time, so it never repeats them.
{\"title\": \"<a few words naming what the call was about>\", \"summary\": \"<the note>\"}"

rules: [
    "Both fields are always present, and summary is never empty.",
    "title: at most eight words, in the same language as summary, naming the subject of the call; no date or time, no emoji, no quotation marks, no full stop at the end.",
]