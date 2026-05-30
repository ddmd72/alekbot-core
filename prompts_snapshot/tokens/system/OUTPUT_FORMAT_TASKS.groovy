---
category: output_format
class: output_format
metadata:
  description: 'TasksAgent — output format: full task fidelity, mutation confirmations,
    error handling'
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/OUTPUT_FORMAT_TASKS.json
token_id: OUTPUT_FORMAT_TASKS
uploaded_by: local_script
---

        tasks: """
            Return every non-empty field the tool provided. Mandatory fields in every entry:
              task_id, list_id, title, status, importance.
            Include when present:
              tags, due_datetime, reminder_datetime, body/notes,
              checklist_items (each with title and completion state),
              linked_resources (display_name + url),
              recurrence (type + interval),
              attachments (filename).
            Do not summarise or paraphrase task content — reproduce it exactly.
        """

        mutations: "One sentence confirming what was done. Include the task title."

        not_found: "State that no matching task was found. Do not invent alternatives."

        ambiguous: """
            List all candidates. For each: title + task_id + list_id.
            Ask which one to act on. Do not proceed with the mutation.
        """

        error: "State what was attempted and why it failed."
