# Handlers — traps

- **`WorkerHandler` is the only `/worker` dispatcher:** an unknown `task_type` is a 400, with no Slack fallthrough. A new
  task type is added to its dispatch and to `docs/07_deployment/SCHEDULERS.md`; `drive_sync` belongs to `indexer_main.py`.
- **`interactive_delivery`** is set only by `ConversationHandler` on a chat turn; owner-only tools (`draft_skill`,
  `propose_memory_facts`, drive note writers) key on it, so a background path (`notify`, `ask_alek`, `send_to_chat`, `/worker`) never sets it.
- **`UserBotConfig.consolidation_threshold` has no effect:** the window is global (`src/domain/settings.py`);
  `consolidation_batch_size` only sizes the `$consolidate` cut.
- **`UserBotConfig.voice_languages` is ISO-639-1, not `LanguageCode`** (that enum has no `ru`). A voice message is transcribed
  before the file pipeline, so its transcript is the user's turn (`decisions/voice_message_transcription.md`).
