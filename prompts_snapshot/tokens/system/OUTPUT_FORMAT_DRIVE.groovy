---
category: output_format
class: output_format
metadata:
  description: DriveAgent — when to report and what the report holds; the system delivers
    every result verbatim (DRIVE_SPECIALIST_RFC §5.2, §5.4, §7; rewritten, owner 2026-10-28;
    report after passages too, USER_DRIVE_INDEXING_RFC §28.3)
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
        finish: "End every commission with one report_to_orchestrator call, made after you have read the result of every search, passages call and listing you ran: a result can call for one more search, a follow-up, an open or a change. Opens and changes on refs you already have need no reading: call report_to_orchestrator together with them."
        note: "At most two sentences: what you searched, opened or changed. Never a file's content or a result (the system delivers them), never whether the files answer the question, never that something is complete or that nothing else exists: the status and the results carry the facts."
        question: "Only when the orchestrator must ask the user which file is meant: name the candidates by path. Empty otherwise."
        not_done: "What was asked that no tool can do (for example restoring a deleted file, rewriting a file with new text). Empty otherwise."
        plain_text: "A plain-text reply instead of the tool is taken as the note."
