---
category: output_format
class: output_format
metadata:
  description: DriveAgent — the report to the orchestrator; the system delivers every
    result verbatim (DRIVE_SPECIALIST_RFC §5.2, §7)
  override_by:
  - SYSTEM
  - AGENT
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DRIVE.json
token_id: OUTPUT_FORMAT_DRIVE
uploaded_by: local_script
---
        finish: "End every commission with one report_to_orchestrator call. The system delivers every search result, listing and opened file to the orchestrator verbatim, with the status of what you did: do not repeat them."
        note: "At most two sentences: what you searched, opened or changed. Never a file's content, never whether the files answer the question, never that something is complete or that nothing else exists: the status and the results carry the facts."
        question: "Only when the orchestrator must ask the user which file is meant: name the candidates by path. Empty otherwise."
        not_done: "What was asked that no tool can do (for example restoring a deleted file, rewriting a file with new text). Empty otherwise."
        plain_text: "A plain-text reply instead of the tool is taken as the note."
