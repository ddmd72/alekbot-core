# Services — traps

- **Text the bot did not write is sent literally** (a stored fact, a drive path, a translated acknowledgement):
  `notify_raw(literal=True)` / `send_literal`, never through the formatter agent, which would rewrite or link it.
- **`_Transcripts` belongs to the bot:** the indexer overwrites every `.md` in it, so `UserDriveService` refuses chat writes
  into the folder; keep that guard (`docs/05_building_blocks/user_drive/README.md` §4.23).
