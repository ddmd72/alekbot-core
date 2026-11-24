---
category: output_format
class: output_format
metadata:
  description: ExternalTaskAgent — how a run ends and what the note holds; the system
    places every tool result before the note (EXTERNAL_TASK_QUEUE_RFC §5.3)
  override_by:
  - SYSTEM
  - AGENT
token_id: OUTPUT_FORMAT_EXTERNAL_TASKS
---
        finish: "When the calls are done, end with a short plain-text note and no tool call. The system places every tool result before your note, word for word, so the orchestrator already has the ids, statuses, lists and results."
        note: "At most two sentences: what you did, and what the results do not say (a task you did not create, and why). Never repeat a result or a worker's text; never a command."
        question: "When the instruction was ambiguous (which worker, which task), the note asks the one question the orchestrator must put to the owner, naming the candidates by label, or by id and title."
