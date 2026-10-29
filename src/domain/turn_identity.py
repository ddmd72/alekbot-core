"""
The id of the owner turn a run serves (DRIVE_FILE_NOTES_RFC §4.2, §4.4; core contract §6).

A run's context carries it under TURN_ID_CONTEXT_KEY, fixed by the entry point BEFORE the orchestrator runs, so every
attempt of the same turn sees the same id: a cross-provider failover re-runs Smart with the same context, and a Cloud
Task redelivery carries the context it was enqueued with. Writers: ConversationHandler (a chat message: the platform's
turn id `slack:<event_id>` / `telegram:<update_id>`, else a fresh `chat:<uuid>`), LelikAgent (one delegation of a call:
`voice:<uuid>`, carried into the `send_to_chat` Cloud Task) and AlekGatewayAgent (keeps the delegation's id, else
`<run_origin>:<task_id>`). Reader: the drive-note tool's provenance (the undo re-run guard is keyed on it).
"""

TURN_ID_CONTEXT_KEY = "turn_id"

__all__ = ["TURN_ID_CONTEXT_KEY"]
